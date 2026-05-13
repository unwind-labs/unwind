"""JSONL parsing for Claude Code session logs.

We treat each ``~/.claude/projects/<slug>/<session-id>.jsonl`` as an append-only
structured log. Each line is independently valid JSON; malformed lines are
skipped. The most useful record shapes we care about:

- ``type == "user"`` / ``"assistant"`` — conversation messages
- ``type == "attachment"`` — hook / system injections
- ``type == "file-history-snapshot"`` — editor state, usually ignorable

Messages carry an intra-session ``parentUuid`` that threads them; we don't rely
on it for rendering, since the JSONL is written in order and we render in order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from ._cache import PathCache as _PathCache


# --- raw line iteration --------------------------------------------------


def iter_lines(path: Path) -> Iterator[dict[str, Any]]:
    """Yield parsed records from a JSONL file, skipping malformed lines."""
    try:
        fh = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield json.loads(raw)
            except json.JSONDecodeError:
                continue


def _load_records(path: Path) -> tuple[dict[str, Any], ...]:
    """Read every line of ``path`` into a tuple of parsed records.

    Tuple (not list) so callers can't mutate the cached payload.
    """
    return tuple(iter_lines(path))


_RECORDS_CACHE = _PathCache(_load_records)


def read_records(path: Path) -> tuple[dict[str, Any], ...]:
    """Cached version of ``list(iter_lines(path))``, keyed by (mtime, size).

    The returned tuple is shared by reference — do not mutate the records
    themselves. Treat them as read-only views of the on-disk JSONL.
    """
    return _RECORDS_CACHE.get(path)  # type: ignore[return-value]


def _collect_uuids_uncached(path: Path) -> frozenset[str]:
    out: set[str] = set()
    for rec in iter_lines(path):
        u = rec.get("uuid")
        if isinstance(u, str):
            out.add(u)
    return frozenset(out)


_UUID_CACHE = _PathCache(_collect_uuids_uncached)


def collect_uuids(path: Path) -> set[str]:
    """Return every ``uuid`` field present in a JSONL.

    Used to compute fork-inheritance: a fork session's JSONL begins with the
    parent's history, sharing message uuids. Any uuid in the fork that is also
    in the parent is "inherited"; the rest is fork-original.

    Cached by (path, mtime, size); the returned set is a frozenset shared by
    reference across callers — must not be mutated.
    """
    return _UUID_CACHE.get(path)  # type: ignore[return-value]


def iter_lines_from(path: Path, byte_offset: int) -> tuple[Iterator[dict[str, Any]], int]:
    """Yield records starting at ``byte_offset``; return new offset.

    Helper used by the watcher to tail growing files without re-reading.
    """
    new_offset = byte_offset
    records: list[dict[str, Any]] = []
    try:
        with path.open("rb") as fh:
            fh.seek(byte_offset)
            buf = fh.read()
            new_offset = byte_offset + len(buf)
    except OSError:
        return iter(records), byte_offset
    for raw in buf.splitlines():
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return iter(records), new_offset


# --- summary extraction (for the session list) ---------------------------


@dataclass(frozen=True)
class SessionSummary:
    """Cheap-to-compute per-session metadata for the left pane."""

    session_id: str
    title: str
    first_timestamp: Optional[datetime]
    last_timestamp: Optional[datetime]
    message_count: int
    file_size_bytes: int
    cwd: Optional[str]
    git_branch: Optional[str]
    custom_title: Optional[str] = None


_META_TYPES = {
    "attachment",
    "file-history-snapshot",
    "permission-mode",
    "last-prompt",
    "system",
}


def extract_session_summary(path: Path, session_id: str) -> Optional[SessionSummary]:
    """Read enough lines to build a ``SessionSummary`` cheaply.

    We scan every line because line count *is* the message count we want to
    show. Individual lines are tiny (~1KB avg), so even 10MB files parse in
    <100ms and this is cached by the sessions layer.
    """
    first_user_text: Optional[str] = None
    first_ts: Optional[datetime] = None
    last_ts: Optional[datetime] = None
    cwd: Optional[str] = None
    git_branch: Optional[str] = None
    custom_title: Optional[str] = None
    message_count = 0

    for rec in iter_lines(path):
        rtype = rec.get("type")

        if rtype == "custom-title":
            ct = rec.get("customTitle")
            if isinstance(ct, str) and ct.strip():
                custom_title = ct.strip()
            continue

        if rtype in _META_TYPES:
            continue

        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            if first_ts is None:
                first_ts = ts
            last_ts = ts

        if cwd is None:
            cwd_val = rec.get("cwd")
            if isinstance(cwd_val, str):
                cwd = cwd_val

        if git_branch is None:
            branch_val = rec.get("gitBranch")
            if isinstance(branch_val, str):
                git_branch = branch_val

        if rtype in ("user", "assistant"):
            message_count += 1
            if first_user_text is None and rtype == "user":
                first_user_text = _extract_user_text(rec)

    try:
        st = path.stat()
        size = st.st_size
    except OSError:
        st = None
        size = 0

    # Fall back to file birthtime / ctime when the JSONL hasn't yet written a
    # record carrying a ``timestamp`` field — e.g. a freshly-created session
    # with only the leading ``permission-mode`` line. This keeps brand-new
    # sessions visible at the top of the list instead of sinking to the bottom
    # with a None first_timestamp.
    if first_ts is None and st is not None:
        first_ts = _file_birth_dt(st)
    if last_ts is None:
        last_ts = first_ts

    title = custom_title or _normalize_title(first_user_text) or session_id[:8]

    return SessionSummary(
        session_id=session_id,
        title=title,
        first_timestamp=first_ts,
        last_timestamp=last_ts,
        message_count=message_count,
        file_size_bytes=size,
        cwd=cwd,
        git_branch=git_branch,
        custom_title=custom_title,
    )


def _file_birth_dt(st) -> Optional[datetime]:
    bt = getattr(st, "st_birthtime", None)
    if isinstance(bt, (int, float)) and bt > 0:
        return datetime.fromtimestamp(bt, tz=timezone.utc)
    if st.st_ctime > 0:
        return datetime.fromtimestamp(st.st_ctime, tz=timezone.utc)
    return None


def stringify_tool_result(r: Any) -> str:
    """Flatten a Claude ``tool_result`` content payload to plain text.

    Accepts ``None``, a plain string, or the list-of-blocks shape that
    Claude emits when the result has structure. Returns an empty
    string for anything else; callers that want richer rendering
    (e.g. JSON pretty-printing) can layer on top.
    """
    if r is None:
        return ""
    if isinstance(r, str):
        return r
    if isinstance(r, list):
        parts: list[str] = []
        for block in r:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def extract_assistant_text(rec: dict[str, Any]) -> Optional[str]:
    """Return the concatenated plain-text content of an assistant record.

    Accepts both the simple string-content shape and the list-of-blocks
    shape; returns ``None`` when the record carries no plain text (e.g.
    only tool_use blocks).
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts) if parts else None
    return None


def parse_ts(raw: Any) -> Optional[datetime]:
    """Parse a Claude-style ISO 8601 timestamp into a UTC ``datetime``.

    Accepts both ``"...Z"`` and ``"...+HH:MM"`` shapes. Returns ``None``
    for non-string or unparseable inputs. Always returns a tz-aware
    UTC ``datetime`` so callers can compare freely.
    """
    if not isinstance(raw, str):
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        return datetime.fromisoformat(raw).astimezone(timezone.utc)
    except ValueError:
        return None


# Module-private alias retained for backwards compatibility within
# ``jsonl.py`` itself.
_parse_ts = parse_ts


def _extract_user_text(rec: dict[str, Any]) -> Optional[str]:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return " ".join(parts) if parts else None
    return None


def _normalize_title(text: Optional[str]) -> Optional[str]:
    if not text:
        return None
    # Strip common wrappers: slash commands, leading/trailing whitespace, newlines.
    cleaned = text.strip().replace("\n", " ")
    if len(cleaned) > 140:
        cleaned = cleaned[:137] + "…"
    return cleaned or None
