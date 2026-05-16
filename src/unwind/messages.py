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
    # Per-child call type (parallel to spawn_session_ids). Values:
    # "fork" | "fresh" | "fresh_cross_project". Drives the icon Unwind
    # renders per spawn row. Only meaningful when ``spawn_kind == "call"``;
    # subagent rows fill with "fork" by convention and ignore the field.
    spawn_call_types: list[str] = field(default_factory=list)

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




from .spawns import Spawn, SpawnResolver  # noqa: E402


def annotate_spawns(
    messages: list[Message],
    slug_callstack=None,
    *,
    current_session_id: Optional[str] = None,
    subagent_index=None,
    fork_detector=None,
    spawn_resolver: Optional[SpawnResolver] = None,
) -> None:
    """Tag tool_use messages with their spawned children's session ids.

    Pass ``spawn_resolver`` (preferred) for one-shot resolution. The
    resolver consolidates callstack reports, fork detector, and the
    subagent index — see :mod:`unwind.spawns`.

    The legacy parameters ``slug_callstack``, ``subagent_index``, and
    ``fork_detector`` are accepted for backwards compatibility (notably
    in tests). When ``spawn_resolver`` is None, an ad-hoc resolver is
    composed from whichever of those were provided. Anything missing is
    handled by best-effort no-op stand-ins.
    """
    if not current_session_id:
        # Without the parent's session id we can't anchor anything.
        # Old behaviour: silently no-op.
        return

    resolver = spawn_resolver or _resolver_from_legacy_args(
        slug_callstack, fork_detector, subagent_index
    )
    if resolver is None:
        return

    spawns = resolver.anchor_to_messages(current_session_id, messages)
    spawns_by_tu: dict[str, list[Spawn]] = {}
    for s in spawns:
        if s.parent_tool_use_id:
            spawns_by_tu.setdefault(s.parent_tool_use_id, []).append(s)

    for m in messages:
        if m.role != "tool_use" or not m.tool_use_id:
            continue
        bound = spawns_by_tu.get(m.tool_use_id)
        if not bound:
            continue
        # All bound spawns for one tool_use share the same kind
        # (callstack tool_uses bind to call-spawns; Agent tool_uses bind
        # to subagent-spawns). Take the first.
        m.spawn_kind = bound[0].kind
        m.spawn_session_ids = [s.child_session_id for s in bound]
        m.spawn_tasks = [s.label for s in bound]
        m.spawn_call_types = [s.call_type for s in bound]
        # Prefer the LATEST known status across all reports for callstack
        # spawns — covers the "original call yielded, later resume
        # completed" case where the spawn's snapshot status is stale.
        m.spawn_done = [_done_for_spawn(s, slug_callstack) for s in bound]


def _resolver_from_legacy_args(
    slug_callstack, fork_detector, subagent_index
) -> Optional[SpawnResolver]:
    """Compose a resolver from the legacy keyword args (used by tests)."""
    from .callstack import CallstackIndex
    from .fork_detect import ForkDetector
    from .subagents import SubagentIndex

    if slug_callstack is None and fork_detector is None and subagent_index is None:
        return None

    # Tests sometimes pass only one source; fill the rest with empty
    # stand-ins rooted at a non-existent path so they're always empty.
    from pathlib import Path as _Path
    sentinel = _Path("/dev/null/no-data")

    cs = slug_callstack if slug_callstack is not None else CallstackIndex(sentinel)
    fd = fork_detector if fork_detector is not None else ForkDetector(sentinel)
    sa = subagent_index if subagent_index is not None else SubagentIndex(sentinel)

    # Project dir: derive from whichever real source we got, else sentinel.
    project_dir = sentinel
    for src in (subagent_index, fork_detector):
        attr = getattr(src, "_project_dir", None)
        if attr is not None:
            project_dir = attr
            break

    return SpawnResolver(cs, fd, sa, project_dir=project_dir)


def _done_for_spawn(s: Spawn, slug_callstack) -> Optional[bool]:
    """Map a Spawn's status to the spawn-row done flag.

    For callstack spawns, prefer the LATEST status aggregated across
    all reports (handles the "original yielded → resume completed"
    case). Falls back to the spawn's snapshot status.
    """
    status = s.status
    if (
        s.kind == "call"
        and s.child_session_id
        and slug_callstack is not None
    ):
        latest = slug_callstack.aggregate_status_for_session(s.child_session_id)
        if latest:
            status = latest
    return _done_from_status(status)


def _done_from_status(status: Optional[str]) -> Optional[bool]:
    """Map a callstack task status into the spawn-row done flag.

    From the PARENT's perspective, a CALL row is "done" the moment the
    child returned control — including when the child yielded for user
    input. The yield is a return; the parent's row should drop the
    in-progress dots once that happens.

    Returns True for terminal states, False for genuinely in-flight
    states, None when unknown.
    """
    if not status:
        return None
    s = status.lower()
    if s in ("complete", "yielded", "error", "failed"):
        return True
    if s in ("running", "in_progress", "pending"):
        return False
    return None


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


