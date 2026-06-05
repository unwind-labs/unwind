"""Normalize raw Claude JSONL into UI-friendly message records.

The frontend consumes a list of ``Message`` objects. We collapse:

- ``type == "user"`` raw text → role=user
- ``type == "user"`` with tool_result blocks → role=tool_result (attached to a prior tool_use)
- ``type == "assistant"`` with text/tool_use blocks → role=assistant
- meta types (attachment, snapshot, permission-mode, last-prompt, system) → skipped unless requested

Each tool_use block is surfaced as its own pseudo-message so the UI can render
it as a collapsible card with its eventual ``tool_result`` paired in.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from .jsonl import parse_ts as _parse_ts, read_records
from .jsonl import stringify_tool_result as _stringify_result
from .spawns import (
    _INVOKE_ID_RE,
    CALLSTACK_AWAITING_TOOL_NAMES,
    CALLSTACK_TOOL_NAMES,
    SUBAGENT_TOOL_NAMES,
    WORKFLOW_TOOL_NAMES,
    CallSpawn,
    Spawn,
    SpawnResolver,
)
from .status import Status, from_raw as _from_raw_status


Role = Literal["user", "assistant", "thinking", "tool_use", "tool_result", "system"]

# Surfaced in place of an empty body for ``redacted_thinking`` blocks
# (encrypted reasoning that has no human-readable ``thinking`` field).
# Asserted verbatim in tests, so this is an implicit UI contract.
REDACTED_THINKING_PLACEHOLDER = "[redacted thinking]"

# Surfaced for ``thinking`` blocks whose ``thinking`` text is empty but that
# carry a ``signature`` — Claude Opus 4.7's extended-thinking response can
# include the encrypted reasoning signature without an accompanying summary.
# Without this the UI shows the generic "(empty)" body, which reads like a
# bug. Asserted verbatim in tests.
ENCRYPTED_THINKING_PLACEHOLDER = "[encrypted thinking]"


@dataclass
class Message:
    uuid: str
    session_id: str
    role: Role
    timestamp: Optional[datetime]
    text: Optional[str] = None

    # For tool_use
    tool_name: Optional[str] = None
    tool_input: Optional[Any] = None
    tool_use_id: Optional[str] = None

    # For tool_result
    tool_result_for: Optional[str] = None  # the tool_use_id being answered
    tool_result: Optional[Any] = None
    is_error: bool = False

    # Grouping: assistant messages may include multiple tool_use blocks. We
    # emit the text first as role=assistant, then one message per tool_use.
    order_within_line: int = 0

    model: Optional[str] = None
    raw_type: Optional[str] = None

    # When this is a tool_use for a callstack invoke* / Agent call, contains
    # the session_ids the call produced. Used by the inline-trace UI to
    # expand into the spawned children's traces.
    spawn_kind: Optional[str] = None  # "call" | "subagent" | None
    spawn_session_ids: list[str] = field(default_factory=list)
    # Parallel array of task labels per spawn child (e.g. ["/task-b",
    # "/task-c", ...]). Length matches the REQUESTED number of children;
    # entries whose session_id hasn't been resolved yet have an empty string
    # in spawn_session_ids but a real task name here.
    spawn_tasks: list[str] = field(default_factory=list)
    # Per-child canonical status (parallel to spawn_session_ids). Lets the
    # caller card render each child's outcome distinctly (done/live/yield/
    # failed) even when the parent ``invoke_parallel`` tool_use is still
    # in flight waiting on slow siblings. ``None`` = unknown (fall back to
    # parent tool_result arrival).
    spawn_status: list[Optional[Status]] = field(default_factory=list)
    # Per-child call type (parallel to spawn_session_ids). Values:
    # "fork" | "fresh" | "fresh_cross_project". Drives the icon Unwind
    # renders per spawn row. Only meaningful when ``spawn_kind == "call"``;
    # subagent rows fill with "fork" by convention and ignore the field.
    spawn_call_types: list[str] = field(default_factory=list)
    # True when this tool_use *references* an already-running invocation
    # instead of *spawning* a new one — i.e. ``await_call`` polling a
    # background ``call``'s invoke_id. The row still renders as a CALL
    # row anchored to the same child, but the canvas-window assignment
    # in the frontend must PEEK an existing window rather than POPPING
    # (the originating ``call`` already claimed the only window for
    # this child; popping again would leave the await row unanchored).
    spawn_is_follower: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("order_within_line", None)
        ts = self.timestamp
        d["timestamp"] = ts.isoformat() if ts else None
        return d


@dataclass
class MessagePage:
    messages: list[Message] = field(default_factory=list)
    last_uuid: Optional[str] = None
    file_offset: int = 0


def base_uuid(message_uuid: str) -> str:
    """Strip the ``:N`` block-index suffix added by the normalizer."""
    if ":" in message_uuid:
        return message_uuid.split(":", 1)[0]
    return message_uuid


def _spawn_kind_for_tool(name: Optional[str]) -> Optional[str]:
    """Classify a tool_use by its tool name. Returns ``"call"`` for
    callstack /call MCP tools, ``"subagent"`` for Agent/Task, ``None``
    otherwise. Pure name lookup — no resolution needed, so it's safe
    to populate at parse time."""
    if not name:
        return None
    if name in CALLSTACK_TOOL_NAMES:
        return "call"
    if name in SUBAGENT_TOOL_NAMES:
        return "subagent"
    if name in WORKFLOW_TOOL_NAMES:
        return "workflow"
    return None


def annotate_spawns(
    messages: list[Message],
    slug_callstack=None,
    *,
    current_session_id: Optional[str] = None,
    spawn_resolver: SpawnResolver,
) -> None:
    """Tag tool_use messages with their spawned children's session ids.

    ``spawn_resolver`` is mandatory — it consolidates callstack reports,
    fork detector, and the subagent index in one place (see
    :mod:`unwind.spawns`). ``slug_callstack`` stays optional because
    only one downstream step (the latest-aggregated-status override in
    ``status_for_spawn``) needs it directly.
    """
    if not current_session_id:
        # Without the parent's session id we can't anchor anything.
        # Old behaviour: silently no-op.
        return

    resolver = spawn_resolver

    spawns = resolver.anchor_to_messages(current_session_id, messages)
    spawns_by_tu: dict[str, list[Spawn]] = {}
    # Side-channel for ``await_call`` decoration: a CallSpawn appears here
    # under EVERY invoke_id it carries (the originating ``call`` was
    # already bound to its tool_use; ``await_call`` rows reference the
    # SAME spawn by invoke_id without claiming it). Built once, reused
    # per await_call message below.
    callspawns_by_invoke: dict[str, list[CallSpawn]] = {}
    for s in spawns:
        if s.parent_tool_use_id:
            spawns_by_tu.setdefault(s.parent_tool_use_id, []).append(s)
        if isinstance(s, CallSpawn) and s.invoke_id:
            callspawns_by_invoke.setdefault(s.invoke_id, []).append(s)

    for m in messages:
        if m.role != "tool_use" or not m.tool_use_id:
            continue
        bound = spawns_by_tu.get(m.tool_use_id)
        if not bound:
            # await_call doesn't claim a spawn (the originating `call`
            # owns parent_tool_use_id), so it never appears in spawns_by_tu.
            # Decorate it by invoke_id instead — same children, same
            # status, so the row shows the running/done child node and
            # the UI can drill into it from either tool_use.
            if (m.tool_name or "") in CALLSTACK_AWAITING_TOOL_NAMES:
                invoke_id = _invoke_id_for_await(m)
                if invoke_id:
                    follow = callspawns_by_invoke.get(invoke_id)
                    if follow:
                        # CallSpawn is a subtype of Spawn; widen for the
                        # invariant list parameter.
                        _decorate_with_spawns(
                            m, list(follow), slug_callstack
                        )
                        # Flag so the frontend's window-assignment peeks
                        # rather than popping (would steal the original
                        # call row's canvas anchor and leave the await
                        # row showing no node).
                        m.spawn_is_follower = True
            continue
        _decorate_with_spawns(m, bound, slug_callstack)


def _decorate_with_spawns(
    m: Message, bound: list[Spawn], slug_callstack
) -> None:
    """Populate a tool_use message's spawn_* fields from a list of Spawns.

    Factored out so ``await_call`` rows (decorated via invoke_id without
    claiming the spawn) share the same projection as ``call``/Agent rows
    (bound via parent_tool_use_id).
    """
    # All bound spawns for one tool_use share the same kind
    # (callstack tool_uses bind to call-spawns; Agent tool_uses bind
    # to subagent-spawns). Take the first.
    m.spawn_kind = bound[0].kind
    m.spawn_session_ids = [s.child_session_id for s in bound]
    m.spawn_tasks = [s.label for s in bound]
    # call_type is only meaningful for CallSpawn; subagent rows
    # default to "fork" (UI ignores call_type for subagent kind).
    m.spawn_call_types = [
        s.call_type if isinstance(s, CallSpawn) else "fork" for s in bound
    ]
    # Prefer the LATEST known status across all reports for callstack
    # spawns — covers the "original call yielded, later resume
    # completed" case where the spawn's snapshot status is stale.
    m.spawn_status = [status_for_spawn(s, slug_callstack) for s in bound]


def _invoke_id_for_await(m: Message) -> Optional[str]:
    """Extract the invoke_id an ``await_call`` tool_use refers to.

    Read from tool_input first (always present — it's the required
    parameter); fall back to the result envelope for paranoia /
    historical envelopes where the input wasn't captured.
    """
    ti = m.tool_input
    if isinstance(ti, dict):
        v = ti.get("invoke_id")
        if isinstance(v, str) and v:
            return v
    # tool_result envelope carries it too — same regex the invoke index uses.
    text = _stringify_result(m.tool_result)
    mt = _INVOKE_ID_RE.search(text)
    return mt.group(1) if mt else None


def status_for_spawn(s: Spawn, slug_callstack) -> Optional[Status]:
    """Return a Spawn's canonical status (``done|live|yield|failed`` or ``None``).

    Single source of truth used by both per-tool_use ``spawn_status``
    decoration (this module) and the unanchored ``SpawnCard`` builder
    (``api/sessions_api.py``). Anywhere else that wants "what is this
    spawn's status?" should call this function — never re-derive from
    raw fields.

    For callstack spawns, prefers the LATEST aggregated status across
    all reports (handles the "original yielded → resume completed"
    case). Falls back to the spawn's snapshot status.

    Asymmetry: we only escalate to ``aggregate_status_for_session``
    when the spawn's OWN status is non-terminal. Once the spawn knows
    its own terminal verdict (e.g. Fix A overrode ``running`` →
    ``complete`` from the child JSONL's ``op:return``), trusting the
    aggregate would flip it BACK to ``live`` for backgrounded calls
    whose ``report.yaml`` is still frozen at ``running``. A returned
    or failed callstack invocation cannot have a genuinely live
    descendant — same invariant the terminal-ancestor wall enforces
    in ``CallstackIndex.aggregate_status_for_session`` itself.
    """
    canonical = _from_raw_status(s.status)
    if (
        canonical not in ("done", "yield", "failed")
        and isinstance(s, CallSpawn)
        and s.child_session_id
        and slug_callstack is not None
    ):
        latest = slug_callstack.aggregate_status_for_session(s.child_session_id)
        if latest is not None:
            canonical = latest
    return canonical


def read_messages(
    path: Path,
    *,
    include_meta: bool = False,
) -> MessagePage:
    """Parse the whole JSONL, returning a normalized ``MessagePage``.

    Records are pulled from ``jsonl.read_records`` (cached by mtime+size),
    so a session that hasn't changed since the last call skips both disk
    I/O and JSON parsing. Normalization runs every call so each
    ``Message`` returned is a fresh, mutable object — safe for callers
    that tag origin/inheritance flags downstream.
    """
    out: list[Message] = []
    last_uuid: Optional[str] = None
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    for rec in read_records(path):
        last_uuid = rec.get("uuid") or last_uuid
        for m in _normalize_record(rec, include_meta=include_meta):
            out.append(m)

    return MessagePage(messages=out, last_uuid=last_uuid, file_offset=size)


def normalize_records(
    records: Iterable[dict[str, Any]], *, include_meta: bool = False
) -> list[Message]:
    out: list[Message] = []
    for rec in records:
        out.extend(_normalize_record(rec, include_meta=include_meta))
    return out


# --- internals -----------------------------------------------------------


_META_ROLES = {"attachment", "file-history-snapshot", "permission-mode", "last-prompt"}


def _normalize_record(
    rec: dict[str, Any], *, include_meta: bool
) -> list[Message]:
    rtype = rec.get("type")

    if rtype in _META_ROLES and not include_meta:
        return []

    uuid = rec.get("uuid") or ""
    session_id = rec.get("sessionId") or ""
    ts = _parse_ts(rec.get("timestamp"))

    msg = rec.get("message") if isinstance(rec.get("message"), dict) else None
    content = msg.get("content") if msg else None

    if rtype == "system":
        if not include_meta:
            return []
        text = rec.get("content") if isinstance(rec.get("content"), str) else None
        return [
            Message(
                uuid=uuid, session_id=session_id, role="system",
                timestamp=ts, text=text, raw_type=rtype,
            )
        ]

    if rtype == "user":
        return _normalize_user(uuid, session_id, ts, content, rec)

    if rtype == "assistant":
        return _normalize_assistant(uuid, session_id, ts, content, msg, rec)

    if rtype in _META_ROLES and include_meta:
        return [
            Message(
                uuid=uuid, session_id=session_id, role="system", timestamp=ts,
                text=_stringify_meta(rec), raw_type=_meta_subtype(rec, rtype),
            )
        ]

    return []


def _meta_subtype(rec: dict[str, Any], rtype: str) -> str:
    """Surface an attachment's specific subtype (``skill_listing``,
    ``hook_success``, ``mcp_instructions_delta``, …) as ``raw_type`` so the
    UI can dispatch a per-type renderer. Non-attachment meta records keep
    their record type."""
    if rtype == "attachment":
        a = rec.get("attachment")
        if isinstance(a, dict):
            sub = a.get("type")
            if isinstance(sub, str) and sub:
                return sub
    return rtype


def _normalize_user(
    uuid: str,
    session_id: str,
    ts: Optional[datetime],
    content: Any,
    rec: dict[str, Any],
) -> list[Message]:
    # Plain string content → user message.
    if isinstance(content, str):
        return [
            Message(
                uuid=uuid, session_id=session_id, role="user",
                timestamp=ts, text=content, raw_type="user",
            )
        ]
    # Structured content: may contain text blocks OR tool_result blocks.
    if isinstance(content, list):
        out: list[Message] = []
        order = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="user", timestamp=ts,
                        text=block.get("text") or "",
                        order_within_line=order,
                        raw_type="user",
                    )
                )
                order += 1
            elif btype == "tool_result":
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="tool_result", timestamp=ts,
                        tool_result_for=block.get("tool_use_id"),
                        tool_result=block.get("content"),
                        is_error=bool(block.get("is_error")),
                        order_within_line=order,
                        raw_type="tool_result",
                    )
                )
                order += 1
        return out
    return []


def _normalize_assistant(
    uuid: str,
    session_id: str,
    ts: Optional[datetime],
    content: Any,
    msg: Optional[dict[str, Any]],
    rec: dict[str, Any],
) -> list[Message]:
    model = (msg or {}).get("model") if isinstance(msg, dict) else None
    out: list[Message] = []
    if isinstance(content, str):
        out.append(
            Message(
                uuid=uuid, session_id=session_id, role="assistant",
                timestamp=ts, text=content, model=model, raw_type="assistant",
            )
        )
        return out
    if isinstance(content, list):
        order = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="assistant", timestamp=ts,
                        text=block.get("text") or "",
                        model=model, raw_type="assistant",
                        order_within_line=order,
                    )
                )
            elif btype in ("thinking", "redacted_thinking"):
                # Four cases, all rendered as role=thinking so the UI stays
                # uniform. Whitespace-only ``thinking`` is treated as empty
                # (it would render as "(empty)" anyway):
                #   - thinking with non-blank text: regular case, pass through.
                #   - redacted_thinking: encrypted ``data``, no readable text
                #     → REDACTED placeholder.
                #   - thinking with blank text + ``signature``: extended
                #     thinking mode where only the encrypted signature comes
                #     back (Opus 4.7 does this routinely) → ENCRYPTED
                #     placeholder.
                #   - thinking with blank text and no signature: malformed-
                #     but-tolerated input → empty string.
                raw_thought = block.get("thinking") or ""
                if raw_thought.strip():
                    thought = raw_thought
                elif btype == "redacted_thinking":
                    thought = REDACTED_THINKING_PLACEHOLDER
                elif block.get("signature"):
                    thought = ENCRYPTED_THINKING_PLACEHOLDER
                else:
                    thought = ""
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="thinking", timestamp=ts,
                        text=thought,
                        model=model, raw_type="thinking",
                        order_within_line=order,
                    )
                )
            elif btype == "tool_use":
                tname = block.get("name")
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="tool_use", timestamp=ts,
                        tool_name=tname,
                        tool_input=block.get("input"),
                        tool_use_id=block.get("id"),
                        model=model, raw_type="tool_use",
                        order_within_line=order,
                        # Eager classification by tool name so the UI can
                        # render the spawn row immediately. ``annotate_spawns``
                        # later fills in ``spawn_session_ids`` etc. once the
                        # resolver has matched the tool_use to a report; the
                        # ``kind`` itself never depends on resolution.
                        spawn_kind=_spawn_kind_for_tool(tname),
                    )
                )
            order += 1
    return out


def _stringify_meta(rec: dict[str, Any]) -> str:
    a = rec.get("attachment")
    if isinstance(a, dict):
        sub = a.get("type")
        # skill_listing's body IS the listing; drop the redundant prefix so
        # the UI can show / count it directly.
        if sub == "skill_listing":
            return str(a.get("content") or "")
        # Tool/instruction deltas carry their payload as name lists, not a
        # ``content`` string — summarise them so the row is skimmable.
        if sub in ("mcp_instructions_delta", "deferred_tools_delta"):
            added = a.get("addedNames")
            if not added and isinstance(a.get("tools"), list):
                added = [t.get("name") for t in a["tools"] if isinstance(t, dict) and t.get("name")]
            removed = a.get("removedNames")
            parts = []
            if added:
                parts.append("added: " + ", ".join(str(x) for x in added))
            if removed:
                parts.append("removed: " + ", ".join(str(x) for x in removed))
            return "\n".join(parts) or str(a.get("content") or "")
        name = a.get("hookName") or sub or ""
        body = a.get("content") or a.get("stdout") or a.get("stderr") or ""
        return f"[{name}] {body}" if name else str(body)
    return str(rec.get("type", ""))


