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

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Literal, Optional

from .jsonl import collect_uuids, iter_lines


Role = Literal["user", "assistant", "tool_use", "tool_result", "system"]


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

    # Fork-aware fields. ``origin_session_id`` is the session that *first*
    # produced this message (i.e. the deepest ancestor that contains its uuid).
    # ``is_inherited`` is True iff origin != self, meaning the message was
    # copied in by ``--fork-session`` and is part of the inherited prefix.
    origin_session_id: Optional[str] = None
    is_inherited: bool = False

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
    # Per-child completion status (parallel to spawn_session_ids). Lets the
    # caller card check off finished children individually, even when the
    # parent ``invoke_parallel`` tool_use is still in flight waiting on
    # slow siblings. ``None`` = unknown (fall back to parent tool_result).
    spawn_done: list[Optional[bool]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "session_id": self.session_id,
            "role": self.role,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "text": self.text,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_use_id": self.tool_use_id,
            "tool_result_for": self.tool_result_for,
            "tool_result": self.tool_result,
            "is_error": self.is_error,
            "model": self.model,
            "raw_type": self.raw_type,
            "origin_session_id": self.origin_session_id,
            "is_inherited": self.is_inherited,
            "spawn_kind": self.spawn_kind,
            "spawn_session_ids": list(self.spawn_session_ids),
            "spawn_tasks": list(self.spawn_tasks),
            "spawn_done": list(self.spawn_done),
        }


@dataclass
class MessagePage:
    messages: list[Message] = field(default_factory=list)
    last_uuid: Optional[str] = None
    file_offset: int = 0
    ancestors: list[str] = field(default_factory=list)


def base_uuid(message_uuid: str) -> str:
    """Strip the ``:N`` block-index suffix added by the normalizer."""
    if ":" in message_uuid:
        return message_uuid.split(":", 1)[0]
    return message_uuid


# Tool names whose use spawns child sessions/agents we want to drill into.
CALLSTACK_TOOL_NAMES = {
    "mcp__plugin_callstack_call__invoke",
    "mcp__plugin_callstack_call__invoke_parallel",
}
SUBAGENT_TOOL_NAMES = {"Agent", "Task"}


_AGENT_ID_RE = __import__("re").compile(r"agentId:\s*([0-9a-f]{8,})")
# Tolerant of JSON-in-string with escaped quotes: matches both "invoke_id":"…"
# and \"invoke_id\":\"…\".
_INVOKE_ID_RE = __import__("re").compile(
    r'\\?"invoke_id\\?"\s*:\s*\\?"([0-9A-Za-z._-]+)\\?"'
)


def annotate_spawns(
    messages: list[Message],
    slug_callstack=None,
    *,
    current_session_id: Optional[str] = None,
    subagent_index=None,
    fork_detector=None,
) -> None:
    """Tag tool_use messages that spawned children with ``spawn_session_ids``.

    Two cases are handled:

    1. Callstack invokes: their ``tool_result`` content embeds an
       ``invoke_id``. We look up that report and attach the child task
       session_ids — but ONLY if the report's ``parent_session`` matches
       ``current_session_id``. This filters out inherited tool_use blocks
       (every fork inherits the parent's invoke_parallel call+result, so
       the invoke_id in their JSONL points at the parent's invocation; we
       don't want to mark inherited messages as spawning anything).
    2. Agent / Task tool calls: the result text contains
       ``agentId: <id>`` — synthesize an ``agent-<id>`` session_id.
       If the tool_result hasn't arrived yet (the subagent is still
       running), fall back to the on-disk subagent index: Claude Code
       creates ``subagents/<session>/agent-<id>.jsonl`` + ``.meta.json``
       as soon as the agent starts, so we can match the pending
       tool_use to its trace by ``description``. Pass
       ``subagent_index`` to enable this.

    Pass ``current_session_id`` whenever you have it (it's the session
    whose JSONL these messages came from). Without it the callstack
    parent-match check is skipped and inherited spawns will be tagged.

    Pass ``fork_detector`` to resolve placeholder session_ids when the
    callstack report doesn't (yet) contain the spawned tasks. The fork
    detector matches each fork's first divergent user message against the
    tool_use's requested task names — robust to callstack reports being
    overwritten by sibling invocations or not yet flushed to disk.
    """
    by_use_id: dict[str, Message] = {}
    by_result_for: dict[str, Message] = {}
    # Preserve chronological order of callstack tool_uses so we can claim
    # reports in order when no tool_result links them by invoke_id.
    callstack_use_order: list[str] = []
    for m in messages:
        if m.role == "tool_use" and m.tool_use_id:
            by_use_id[m.tool_use_id] = m
            if m.tool_name in CALLSTACK_TOOL_NAMES:
                callstack_use_order.append(m.tool_use_id)
        elif m.role == "tool_result" and m.tool_result_for:
            by_result_for[m.tool_result_for] = m

    # Track which reports have been "claimed" by a specific tool_use, so a
    # later in-flight tool_use can pick the NEXT unclaimed report instead of
    # double-counting.
    claimed_invoke_ids: set[str] = set()
    # Track session_ids already attributed to a prior callstack tool_use.
    # Multiple nested ``invoke*`` calls from the same fork share one report
    # (callstack merges them), so without this each tool_use sees the union
    # of every prior invocation's children — and the leftover-task logic
    # below would phantom-render them.
    claimed_child_sids: set[str] = set()
    # Same idea for subagent invocations matched by description.
    claimed_agent_ids: set[str] = set()

    # Iterate callstack uses in chronological order so unclaimed-report
    # selection is stable.
    ordered_items = [(uid, by_use_id[uid]) for uid in callstack_use_order] + [
        (uid, m)
        for uid, m in by_use_id.items()
        if (m.tool_name or "") not in CALLSTACK_TOOL_NAMES
    ]
    for use_id, use_msg in ordered_items:
        name = use_msg.tool_name or ""
        result_msg = by_result_for.get(use_id)
        if name in CALLSTACK_TOOL_NAMES:
            # Match each callstack tool_use to ONE specific report. Without
            # this, a session with multiple invokes (e.g. /run twice) would
            # see all callstack tool_uses point at the merged children of
            # ALL prior invokes.
            #
            # Strategy:
            #  1. If the tool_result is in (call returned), extract invoke_id
            #     and bind to that report exactly.
            #  2. Otherwise (live, in-flight), pick the NEXT unclaimed report
            #     for this session in chronological order.
            tasks: list = []
            chosen_report = None
            invoke_id = _extract_invoke_id(result_msg)
            if invoke_id and slug_callstack is not None:
                report = slug_callstack.report_for_invoke(invoke_id)
                if report is not None and current_session_id is not None:
                    chosen_report = report
                    claimed_invoke_ids.add(report.invoke_id)
                    tasks = slug_callstack.children_in_report(
                        report, current_session_id
                    )
            elif current_session_id is not None and slug_callstack is not None:
                # In-flight (no tool_result yet) — try the first unclaimed
                # report first (legacy: each invocation got its own report).
                for rep in slug_callstack.reports_with_session_node(
                    current_session_id
                ):
                    if rep.invoke_id in claimed_invoke_ids:
                        continue
                    chosen_report = rep
                    claimed_invoke_ids.add(rep.invoke_id)
                    tasks = slug_callstack.children_in_report(
                        rep, current_session_id
                    )
                    break
                # If no unclaimed report exists, fall back to whichever
                # report contains this session — modern callstack merges
                # sibling invocations into one report, so the prior tool_use
                # already claimed it. ``claimed_child_sids`` below filters
                # tasks already attributed to siblings, so name-matching
                # below still resolves THIS tool_use's children.
                if not tasks:
                    for rep in slug_callstack.reports_with_session_node(
                        current_session_id
                    ):
                        chosen_report = rep
                        tasks = slug_callstack.children_in_report(
                            rep, current_session_id
                        )
                        break

            # Strip tasks already claimed by an earlier tool_use so we don't
            # double-count children when the report is shared across multiple
            # nested invokes.
            if claimed_child_sids:
                tasks = [
                    t for t in tasks
                    if not (t.session_id and t.session_id in claimed_child_sids)
                ]

            # Index per-child completion status by session_id so the caller
            # card can mark individual children done before the parent
            # ``invoke_parallel`` tool_result lands.
            status_by_sid: dict[str, str] = {
                t.session_id: (t.status or "").lower()
                for t in tasks if t.session_id
            }

            # Build per-child (session_id, task) pairs, preserving the order
            # requested in the tool_input so unresolved children render as
            # placeholder rows.
            #
            # Match by task NAME, not by index: callstack writes tree.nodes
            # in arrival/completion order, which doesn't always match the
            # requested order during live runs. Mismatched order would
            # otherwise pin the wrong session_id to a task name on the first
            # render, and the canvas's "first label wins" logic would lock
            # the mistake in.
            child_pairs: list[tuple[str, str]] = []
            requested = _requested_tasks(use_msg.tool_input)
            if requested:
                # Bucket tasks by name (preserves duplicates so two requests
                # for "/task-b" each get their own session_id in order).
                by_name: dict[str, list[str]] = {}
                for t in tasks:
                    name = t.task or ""
                    sid = t.session_id or ""
                    if name:
                        by_name.setdefault(name, []).append(sid)
                for t_name in requested:
                    bucket = by_name.get(t_name)
                    sid = bucket.pop(0) if bucket else ""
                    child_pairs.append((sid, t_name))
                # NOTE: we deliberately do NOT append unmatched report tasks
                # as leftovers here. With callstack's merged-frame model, a
                # single report often contains tasks from sibling tool_uses
                # (5 specialists + 3 meta-assessors + 1 re-author all share
                # one report). Appending leftovers would emit phantom rows
                # for tasks that belong to a different tool_use. Sibling
                # tool_uses surface their own children when the loop
                # processes them.
            else:
                # No requested tasks (e.g. Skill-style invoke) — fall back to
                # whatever the chosen report has.
                for t in tasks:
                    child_pairs.append((t.session_id or "", t.task or ""))

            # Fork-detector fallback: any pair still missing a session_id
            # gets resolved by matching the fork's first divergent user text
            # against the requested task name. This survives callstack
            # reports being overwritten by sibling invocations and works for
            # in-flight invocations whose tool_result hasn't landed yet.
            if fork_detector is not None and current_session_id is not None:
                resolved: list[tuple[str, str]] = []
                for sid, t_name in child_pairs:
                    if sid or not t_name:
                        resolved.append((sid, t_name))
                        continue
                    found = fork_detector.find_session_by_divergence_text(
                        current_session_id, t_name
                    )
                    if found and found not in claimed_child_sids:
                        resolved.append((found, t_name))
                    else:
                        resolved.append((sid, t_name))
                child_pairs = resolved

            # Drop empty placeholders.
            child_pairs = [(s, t) for s, t in child_pairs if s or t]
            if child_pairs:
                use_msg.spawn_kind = "call"
                use_msg.spawn_session_ids = [s for s, _ in child_pairs]
                use_msg.spawn_tasks = [t for _, t in child_pairs]
                use_msg.spawn_done = [
                    _done_from_status(status_by_sid.get(s)) if s else None
                    for s, _ in child_pairs
                ]
                for sid, _t in child_pairs:
                    if sid:
                        claimed_child_sids.add(sid)
            _ = chosen_report  # (silences unused; kept for future debug)
        elif name in SUBAGENT_TOOL_NAMES:
            agent_id = _extract_agent_id(result_msg)
            if agent_id:
                use_msg.spawn_kind = "subagent"
                use_msg.spawn_session_ids = [f"agent-{agent_id}"]
                claimed_agent_ids.add(agent_id)
            elif subagent_index is not None and current_session_id:
                # In-flight: the tool_result hasn't been written yet. Match
                # by description against the on-disk subagent invocations.
                desc = ""
                if isinstance(use_msg.tool_input, dict):
                    raw = use_msg.tool_input.get("description")
                    if isinstance(raw, str):
                        desc = raw.strip()
                if desc:
                    pending_id = _claim_pending_subagent(
                        subagent_index, current_session_id, desc, claimed_agent_ids
                    )
                    if pending_id:
                        use_msg.spawn_kind = "subagent"
                        use_msg.spawn_session_ids = [f"agent-{pending_id}"]


def _done_from_status(status: Optional[str]) -> Optional[bool]:
    """Map a callstack task status into the spawn-row done flag.

    Returns True for terminal-success states, False for in-flight states,
    None when unknown — the UI falls back to the parent's tool_result for
    None entries.
    """
    if not status:
        return None
    s = status.lower()
    if s in ("complete",):
        return True
    if s in ("running", "in_progress", "pending", "yielded"):
        return False
    # ``error``/``failed`` count as terminal so the row stops pulsing.
    if s in ("error", "failed"):
        return True
    return None


def _requested_tasks(tool_input: Any) -> list[str]:
    """Return the requested task labels from a callstack invoke* tool_input.

    For ``invoke_parallel(tasks=[...])`` returns the tasks list; for
    ``invoke(task=...)`` returns ``[task]``; otherwise empty.
    """
    if not isinstance(tool_input, dict):
        return []
    tasks = tool_input.get("tasks")
    if isinstance(tasks, list):
        return [str(t) for t in tasks]
    task = tool_input.get("task")
    if isinstance(task, str):
        return [task]
    return []


def _claim_pending_subagent(
    subagent_index,
    session_id: str,
    description: str,
    already_claimed: set[str],
) -> Optional[str]:
    """Match an in-flight Agent tool_use to its on-disk subagent trace.

    Claude Code writes ``subagents/<session>/agent-<id>.meta.json`` as soon as
    the subagent starts, well before the parent's ``tool_result`` lands. So
    while the tool call is pending we can still surface the link by matching
    the tool_use's ``description`` arg against the meta files. Two calls with
    identical descriptions get distinct invocations because we track which
    agent ids have already been claimed in this annotation pass.
    """
    try:
        invocations = subagent_index.list_for_session(session_id)
    except Exception:  # subagent index is best-effort
        return None
    for inv in invocations:
        if inv.agent_id in already_claimed:
            continue
        if inv.description.strip() == description:
            already_claimed.add(inv.agent_id)
            return inv.agent_id
    return None


def _extract_invoke_id(result: Optional[Message]) -> Optional[str]:
    if result is None or result.tool_result is None:
        return None
    text = _stringify_result(result.tool_result)
    m = _INVOKE_ID_RE.search(text)
    return m.group(1) if m else None


def _extract_agent_id(result: Optional[Message]) -> Optional[str]:
    if result is None or result.tool_result is None:
        return None
    text = _stringify_result(result.tool_result)
    m = _AGENT_ID_RE.search(text)
    return m.group(1) if m else None


def _stringify_result(r: Any) -> str:
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        parts: list[str] = []
        for block in r:
            if isinstance(block, dict):
                if block.get("type") == "text" and isinstance(block.get("text"), str):
                    parts.append(block["text"])
        return "\n".join(parts)
    return ""


def read_messages(
    path: Path,
    *,
    include_meta: bool = False,
) -> MessagePage:
    """Parse the whole JSONL, returning a normalized ``MessagePage``."""
    out: list[Message] = []
    last_uuid: Optional[str] = None
    try:
        size = path.stat().st_size
    except OSError:
        size = 0

    for rec in iter_lines(path):
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


def annotate_origins(
    page: MessagePage,
    self_session_id: str,
    ancestor_uuid_sets: list[tuple[str, set[str]]],
) -> None:
    """Tag each message with which ancestor first produced it.

    ``ancestor_uuid_sets`` is ordered immediate-parent-first up to root —
    matching ``CallstackIndex.parent_chain``. A message uuid present in the
    deepest (root-most) ancestor that contains it wins, since that's where
    the message *originated* before being copy-inherited downstream.
    """
    for m in page.messages:
        bid = base_uuid(m.uuid)
        origin = self_session_id
        # Walk ancestors from immediate parent to root. The last (deepest)
        # ancestor that contains this uuid is the origin.
        for ancestor_id, uuids in ancestor_uuid_sets:
            if bid in uuids:
                origin = ancestor_id
        m.origin_session_id = origin
        m.is_inherited = origin != self_session_id


def read_messages_with_lineage(
    self_path: Path,
    self_session_id: str,
    ancestor_paths: list[tuple[str, Optional[Path]]],
    *,
    include_meta: bool = False,
) -> MessagePage:
    """Load this session's messages plus every ancestor's, with origin tags.

    For callstack-style fork chains, intermediate ancestors typically don't
    contribute new conversation context BEFORE forking. So an inherited band
    that only shows messages-attributable-by-uuid sees ~all root content.
    This function instead reads every ancestor's full thread and merges them
    by timestamp, deduping by uuid (deepest ancestor wins, so a message that
    originated in root and was copied down to F is attributed to root, not F).

    ``ancestor_paths`` is ordered immediate-parent-first up to root, matching
    ``CallstackIndex.parent_chain``. Pass ``None`` for any ancestor whose
    JSONL is missing — it's silently skipped.
    """
    out: list[Message] = []
    seen: set[str] = set()
    epoch_zero = datetime.fromtimestamp(0, tz=timezone.utc)

    # Walk ancestors from root downward so root claims uuids first; an
    # intermediate ancestor only "owns" a uuid the root doesn't have.
    for ancestor_id, path in reversed(ancestor_paths):
        if path is None:
            continue
        try:
            page = read_messages(path, include_meta=include_meta)
        except FileNotFoundError:
            continue
        for m in page.messages:
            bid = base_uuid(m.uuid)
            if bid in seen:
                continue
            m.origin_session_id = ancestor_id
            m.is_inherited = True
            out.append(m)
            seen.add(bid)

    # Self last — anything not already claimed by an ancestor is own work.
    self_page = read_messages(self_path, include_meta=include_meta)
    last_uuid = self_page.last_uuid
    file_offset = self_page.file_offset
    for m in self_page.messages:
        bid = base_uuid(m.uuid)
        if bid in seen:
            # Inherited via ancestor — skip the self-side copy; already
            # represented with the proper ancestor attribution.
            continue
        m.origin_session_id = self_session_id
        m.is_inherited = False
        out.append(m)
        seen.add(bid)

    out.sort(key=lambda m: m.timestamp or epoch_zero)
    return MessagePage(messages=out, last_uuid=last_uuid, file_offset=file_offset)


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
                text=_stringify_meta(rec), raw_type=rtype,
            )
        ]

    return []


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
            elif btype == "tool_use":
                out.append(
                    Message(
                        uuid=f"{uuid}:{order}", session_id=session_id,
                        role="tool_use", timestamp=ts,
                        tool_name=block.get("name"),
                        tool_input=block.get("input"),
                        tool_use_id=block.get("id"),
                        model=model, raw_type="tool_use",
                        order_within_line=order,
                    )
                )
            order += 1
    return out


def _stringify_meta(rec: dict[str, Any]) -> str:
    a = rec.get("attachment")
    if isinstance(a, dict):
        name = a.get("hookName") or a.get("type") or ""
        body = a.get("content") or a.get("stdout") or a.get("stderr") or ""
        return f"[{name}] {body}" if name else str(body)
    return str(rec.get("type", ""))


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except ValueError:
        return None
