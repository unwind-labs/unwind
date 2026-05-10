"""Canvas tree builder.

Compute the canvas's window-tree directly from session JSONLs and
callstack ``report.yaml`` files, in a single deterministic pass.

A *window* is a slice of one session's activity bounded by parent
invocations. The parent calls the child K times (initial + K-1
resumes); the child has K windows, one per invocation. Each parent
window is the slice of the parent that contains a particular
invocation timestamp.

This replaces the incremental, race-prone protocol where every
CompactCard emits spawn rows up to the canvas. The new design:

* enumerates all parent → child invocations from callstack reports
* scans each reachable session's JSONL once for yields and bounds
* assigns the K-th invocation to the K-th window of the target
* finds the parent's containing window by timestamp

Producing a single immutable tree the frontend renders directly.
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .jsonl import iter_lines


# MCP tools that map to a parent → child invocation. Surfaced for
# debugging/symmetry with messages.py — actual edges currently come
# from callstack ``report.yaml`` files (more authoritative since they
# carry timestamps + child session ids without needing tool_result
# correlation).
CALLSTACK_TOOL_NAMES = frozenset(
    {
        # Legacy names — kept so old sessions still match. Current runtime
        # emits the unprefixed forms below.
        "mcp__plugin_callstack_call__invoke",
        "mcp__plugin_callstack_call__invoke_parallel",
        "mcp__plugin_callstack_call__invoke_resume",
        "mcp__plugin_callstack_call__call",
        "mcp__plugin_callstack_call__resume",
    }
)

# Yield envelope detector. Claude Code's callstack runtime emits
# ``{"op": "yield", "question": ...}`` inside an assistant message's
# fenced code block when the session pauses for user input. There's no
# atomic record type; the envelope IS the signal.
_YIELD_RE = re.compile(
    r'"op"\s*:\s*"yield"',
)


# --- Data classes -------------------------------------------------------


@dataclass
class SessionScan:
    """Lightweight per-session summary used to build canvas windows."""

    session_id: str
    path: Path
    mtime: float = 0.0
    size: int = 0
    start_ts: Optional[datetime] = None
    end_ts: Optional[datetime] = None
    yields: list[datetime] = field(default_factory=list)
    # True if the session's most recent meaningful event is Claude
    # finishing a turn (``system/stop_hook_summary``) with no user
    # reply since — i.e. Claude is currently waiting for input. This
    # is the "interactive yield" signal that callstack-yield envelopes
    # don't catch (and that ``away_summary`` recaps incorrectly
    # implied).
    at_user_prompt: bool = False


@dataclass
class Invocation:
    """One parent → child invocation discovered via callstack reports."""

    caller_session_id: str
    target_session_id: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    label: str
    status: str  # complete | yielded | running | failed
    kind: str    # ``call``/``invoke`` (legacy) | ``call_resume``/``invoke_resume`` (legacy)
    invoke_id: str


@dataclass
class WindowNode:
    """One slice of a session's activity. Renders as one card on the canvas."""

    window_id: str
    session_id: str
    label: str
    window_start: Optional[datetime]
    window_end: Optional[datetime]
    status: str  # ``done`` | ``live`` | ``yield``
    kind: str    # ``root`` | ``call`` | ``subagent`` | ``resume``
    parent_window_id: Optional[str]
    children: list["WindowNode"] = field(default_factory=list)
    # Index of this window within its session (0-based, chronological).
    # Useful for the frontend to label "1st", "2nd" instances cleanly.
    window_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window_id": self.window_id,
            "session_id": self.session_id,
            "label": self.label,
            "window_start": self.window_start.isoformat()
            if self.window_start
            else None,
            "window_end": self.window_end.isoformat() if self.window_end else None,
            "status": self.status,
            "kind": self.kind,
            "parent_window_id": self.parent_window_id,
            "window_index": self.window_index,
            "children": [c.to_dict() for c in self.children],
        }


# --- Scanning -----------------------------------------------------------


def scan_session(path: Path) -> SessionScan:
    """Walk a session's JSONL once; collect start, end, yield timestamps,
    and the at-user-prompt state (Claude finished its turn, awaiting reply).
    """
    try:
        st = path.stat()
    except OSError:
        return SessionScan(session_id=path.stem, path=path)
    scan = SessionScan(
        session_id=path.stem,
        path=path,
        mtime=st.st_mtime,
        size=st.st_size,
    )
    at_user_prompt = False
    for rec in iter_lines(path):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            if scan.start_ts is None:
                scan.start_ts = ts
            scan.end_ts = ts
        rtype = rec.get("type")
        if rtype == "assistant":
            text = _extract_assistant_text(rec)
            if text and _YIELD_RE.search(text) and ts is not None:
                scan.yields.append(ts)
            # Claude is producing output → not at user prompt.
            at_user_prompt = False
        elif rtype == "user":
            # Tool results are recorded as ``type: user`` with content
            # blocks of type ``tool_result``; those don't reset the
            # waiting state (Claude is processing the tool response).
            # Only an actual user reply (text content) does.
            if not _is_tool_result_record(rec):
                at_user_prompt = False
        elif rtype == "system":
            sub = rec.get("subtype")
            if sub == "stop_hook_summary":
                # Claude wrapped up its turn. Until a user record
                # arrives, the session is waiting for input.
                at_user_prompt = True
    scan.at_user_prompt = at_user_prompt
    return scan


def _is_tool_result_record(rec: dict[str, Any]) -> bool:
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return False
    content = msg.get("content")
    if not isinstance(content, list):
        return False
    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return True
    return False


def _extract_assistant_text(rec: dict[str, Any]) -> Optional[str]:
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


def _parse_ts(raw: Any) -> Optional[datetime]:
    if not isinstance(raw, str):
        return None
    try:
        s = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# --- Invocation enumeration --------------------------------------------


def collect_invocations(
    callstack_index: Any,
    subagent_index: Any = None,
    *,
    known_session_ids: Optional[set[str]] = None,
) -> dict[str, list[Invocation]]:
    """Enumerate every parent → child invocation.

    Two sources:
      * **callstack reports** — ``/call`` invocations with their full
        task tree; one ``Invocation`` per (parent, task) edge per
        report.
      * **subagent index** — ``Agent``/``Task`` tool spawns. These
        live as ``<session>/subagents/agent-<id>.jsonl`` and don't
        appear in callstack reports. We synthesize an Invocation per
        subagent with a synthetic ``agent-<id>`` target session id.

    Returns a map keyed by child ``session_id``. Each list is sorted
    chronologically by ``started_at`` so the K-th entry corresponds to
    the K-th window of the child.

    ``known_session_ids`` (when given) bounds the subagent walk: we
    only enumerate subagents for sessions we'll actually scan,
    avoiding a full filesystem walk for unrelated projects.
    """
    out: dict[str, list[Invocation]] = {}

    def add(caller_sid: str, task: Any, rep: Any) -> None:
        if not task.session_id:
            return
        out.setdefault(task.session_id, []).append(
            Invocation(
                caller_session_id=caller_sid,
                target_session_id=task.session_id,
                started_at=rep.started_at,
                ended_at=rep.ended_at,
                label=task.task or task.session_id[:8],
                status=(task.status or "complete").lower(),
                kind=getattr(rep, "kind", "invoke") if hasattr(rep, "kind") else "invoke",
                invoke_id=rep.invoke_id,
            )
        )

    def visit(node: Any, parent_sid: str, rep: Any) -> None:
        for c in node.children:
            add(parent_sid, c, rep)
            next_parent = c.session_id or parent_sid
            visit(c, next_parent, rep)

    for rep in callstack_index.all_reports():
        for task in rep.tasks:
            add(rep.parent_session, task, rep)
            if task.session_id:
                visit(task, task.session_id, rep)

    # Subagent invocations: one Invocation per (parent_sid, subagent)
    # pair. The target session id is the subagent's synthetic
    # ``agent-<id>``; the canvas builder treats it as a leaf node
    # (single window covering the subagent's recorded activity).
    if subagent_index is not None and known_session_ids:
        for sid in known_session_ids:
            for sa in subagent_index.list_for_session(sid):
                out.setdefault(sa.synthetic_session_id, []).append(
                    Invocation(
                        caller_session_id=sid,
                        target_session_id=sa.synthetic_session_id,
                        started_at=sa.created_at,
                        ended_at=None,
                        label=sa.description or sa.agent_type or sa.agent_id[:8],
                        status="complete",
                        kind="subagent",
                        invoke_id=f"subagent-{sa.agent_id}",
                    )
                )

    epoch = datetime.fromtimestamp(0, timezone.utc)
    for invs in out.values():
        invs.sort(key=lambda i: i.started_at or epoch)
    return out


# --- Window construction -----------------------------------------------


def _compute_windows(
    scan: SessionScan,
    invocations: list[Invocation],
    *,
    is_root: bool,
    is_live: bool,
    title: str,
) -> list[WindowNode]:
    """Build the per-invocation windows for one session.

    Root sessions and sessions with no recorded invocations get a
    single window covering the whole JSONL. Otherwise we emit one
    window per invocation in chronological order.
    """
    if is_root or not invocations:
        end = None if is_live else scan.end_ts
        # ``yield`` is gated on ``is_live`` (process up + recent
        # activity). Sessions that ended their turn long ago are
        # technically "resumable via ``claude --resume``", but the
        # vast majority of historical sessions live in that state —
        # surfacing them all as amber yield would drown out the
        # genuinely-waiting ones. Treat them as ``done`` and let the
        # user resume them out-of-band.
        if is_live and (
            scan.at_user_prompt
            or (
                scan.yields
                and (scan.end_ts is None or scan.yields[-1] >= scan.end_ts)
            )
        ):
            status = "yield"
        elif is_live:
            status = "live"
        else:
            status = "done"
        return [
            WindowNode(
                window_id=f"{scan.session_id}#0",
                session_id=scan.session_id,
                label=title,
                window_start=scan.start_ts,
                window_end=end,
                status=status,
                kind="root" if is_root else "call",
                parent_window_id=None,
                window_index=0,
            )
        ]

    out: list[WindowNode] = []
    n = len(invocations)
    # Prefer the invocation's own ``label`` (the parent's task description,
    # e.g. "Run /verify-mfa for ...") over the session's title (which is
    # the session's first user message — for callstack-spawned children
    # that's typically the entire workflow context, identical across
    # siblings). Fall back to the session title only if the invocation
    # has no label.
    inv_label = invocations[0].label or title
    for k, inv in enumerate(invocations):
        # Window K's end = next invocation's start (parent resumed at
        # that moment) OR this invocation's own end OR open-ended.
        if k + 1 < n and invocations[k + 1].started_at is not None:
            w_end: Optional[datetime] = invocations[k + 1].started_at
        elif k == n - 1 and is_live:
            w_end = None
        else:
            w_end = inv.ended_at

        # Status: only the FINAL window of a session can be "currently
        # waiting" — earlier windows whose task yielded got resumed
        # (that's how a later window came to exist) and are now in the
        # past, so they show as ``done``. The final window's status
        # mirrors the invocation's task status verbatim.
        inv_status = (inv.status or "").lower()
        is_last = k == n - 1
        if not is_last:
            status = "done"
        elif inv_status == "yielded":
            status = "yield"
        elif inv_status in ("running", "in_progress", "pending"):
            status = "live" if is_live else "done"
        else:
            status = "done"

        out.append(
            WindowNode(
                window_id=f"{scan.session_id}#{k}",
                session_id=scan.session_id,
                label=inv_label,
                window_start=inv.started_at,
                window_end=w_end,
                status=status,
                kind="call" if k == 0 else "resume",
                parent_window_id=None,  # set by the wiring step
                window_index=k,
            )
        )
    return out


def _find_window_for_ts(
    windows: list[WindowNode], ts: Optional[datetime]
) -> Optional[WindowNode]:
    """Return the window whose ``[window_start, window_end)`` contains ``ts``."""
    if not windows:
        return None
    if ts is None:
        return windows[0]
    for w in windows:
        if w.window_start is not None and ts < w.window_start:
            continue
        if w.window_end is not None and ts >= w.window_end:
            continue
        return w
    # Past the end of all windows — attribute to the last one. (A late
    # call event from a parent whose child is already done.)
    return windows[-1]


# --- Top-level build ---------------------------------------------------


IsLiveFn = Callable[[str], bool]
TitleFn = Callable[[str], Optional[str]]


def build_canvas_tree(
    project_dir: Path,
    root_session_id: str,
    callstack_index: Any,
    *,
    subagent_index: Any = None,
    builder: Optional["CanvasTreeBuilder"] = None,
    is_live_session: IsLiveFn = lambda _sid: False,
    title_for: TitleFn = lambda _sid: None,
) -> tuple[WindowNode, list[WindowNode]]:
    """Compute the canvas tree rooted at ``root_session_id``.

    Returns ``(root_window, all_windows_flat)``.
    """
    # Pass 1: enumerate callstack invocations and BFS from root to
    # discover reachable real sessions. We need this set BEFORE we can
    # collect subagent invocations (which are keyed by parent session).
    cs_invs = collect_invocations(callstack_index)
    calls_from: dict[str, set[str]] = {}
    for target_sid, invs in cs_invs.items():
        for inv in invs:
            calls_from.setdefault(inv.caller_session_id, set()).add(target_sid)

    real_sessions: set[str] = set()
    queue: list[str] = [root_session_id]
    while queue:
        sid = queue.pop(0)
        if sid in real_sessions:
            continue
        real_sessions.add(sid)
        for child in calls_from.get(sid, ()):
            queue.append(child)

    # Pass 2: re-collect with subagents bounded to reachable sessions.
    # Subagent target ids look like ``agent-<hex>`` and have no JSONL
    # in ``project_dir`` — they're terminal leaves in the tree.
    invocations_by_target = collect_invocations(
        callstack_index,
        subagent_index,
        known_session_ids=real_sessions,
    )

    # Scan every real session (subagents skip the scan — they have no
    # ``<session_id>.jsonl`` in project_dir).
    scans: dict[str, SessionScan] = {}
    for sid in real_sessions:
        if builder is not None:
            scans[sid] = builder.get_scan(sid)
        else:
            path = project_dir / f"{sid}.jsonl"
            scans[sid] = (
                scan_session(path) if path.is_file() else SessionScan(session_id=sid, path=path)
            )
    # Add empty scans for subagent targets so windows_by_session has
    # entries to iterate over below.
    visited = set(real_sessions)
    for target_sid in invocations_by_target:
        if target_sid in visited:
            continue
        visited.add(target_sid)
        scans[target_sid] = SessionScan(
            session_id=target_sid, path=project_dir / f"{target_sid}.jsonl"
        )

    # Build windows per session.
    windows_by_session: dict[str, list[WindowNode]] = {}
    for sid, scan in scans.items():
        invs = invocations_by_target.get(sid, [])
        is_root = sid == root_session_id
        title = title_for(sid) or _short_session_label(sid)
        windows_by_session[sid] = _compute_windows(
            scan,
            invs,
            is_root=is_root,
            is_live=is_live_session(sid),
            title=title,
        )

    # Wire parent → child edges by matching the K-th invocation of a
    # target to the K-th window of that target.
    all_windows: list[WindowNode] = []
    for sid, ws in windows_by_session.items():
        all_windows.extend(ws)

    for target_sid, invs in invocations_by_target.items():
        target_windows = windows_by_session.get(target_sid, [])
        for k, inv in enumerate(invs):
            if k >= len(target_windows):
                continue
            tw = target_windows[k]
            caller_windows = windows_by_session.get(inv.caller_session_id, [])
            cw = _find_window_for_ts(caller_windows, inv.started_at)
            if cw is None:
                continue
            tw.parent_window_id = cw.window_id
            # Override kind based on the invocation's nature:
            #   * subagent invocations stay as "subagent" (preserved
            #     from collect_invocations);
            #   * the first callstack invocation = "call";
            #   * subsequent callstack invocations = "resume".
            if inv.kind != "subagent":
                tw.kind = "call" if k == 0 else "resume"
            else:
                tw.kind = "subagent"
            cw.children.append(tw)

    # Sort each window's children for a stable visual ordering: by
    # the child's own start time (which orders parallel invokes by
    # when they fired, and resume series by their timeline).
    epoch = datetime.fromtimestamp(0, timezone.utc)
    for w in all_windows:
        w.children.sort(key=lambda c: c.window_start or epoch)

    # Root window is index 0 of root_session_id (root sessions always
    # get a single window — see _compute_windows).
    root_windows = windows_by_session.get(root_session_id) or []
    if not root_windows:
        root = WindowNode(
            window_id=f"{root_session_id}#0",
            session_id=root_session_id,
            label=title_for(root_session_id) or _short_session_label(root_session_id),
            window_start=None,
            window_end=None,
            status="done",
            kind="root",
            parent_window_id=None,
            window_index=0,
        )
        all_windows.append(root)
        return root, all_windows
    return root_windows[0], all_windows


def _short_session_label(sid: str) -> str:
    # ``agent-<id>`` synthetic subagent ids: drop the prefix.
    if sid.startswith("agent-"):
        return sid[6:14]
    return sid[:8]


# --- Project-scoped builder (caches scans) -----------------------------


class CanvasTreeBuilder:
    """Per-project scan cache. Reuses scans across canvas requests.

    Re-scans a session only when its JSONL's mtime/size changes.
    """

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._scans: dict[str, SessionScan] = {}
        self._lock = threading.Lock()

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    def get_scan(self, session_id: str) -> SessionScan:
        path = self._project_dir / f"{session_id}.jsonl"
        try:
            st = path.stat()
        except OSError:
            # No JSONL on disk — placeholder scan.
            return SessionScan(session_id=session_id, path=path)
        with self._lock:
            cached = self._scans.get(session_id)
        if (
            cached is not None
            and cached.mtime == st.st_mtime
            and cached.size == st.st_size
        ):
            return cached
        scan = scan_session(path)
        with self._lock:
            self._scans[session_id] = scan
        return scan
