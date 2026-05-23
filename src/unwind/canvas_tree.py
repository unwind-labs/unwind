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

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

import re

from ._cache import PathCache
from .jsonl import (
    EPOCH,
    RETURN_RE as _RETURN_RE,
    YIELD_RE as _YIELD_RE,
    _text_blocks,
    extract_assistant_text as _extract_assistant_text,
    iter_lines,
    parse_ts as _parse_ts,
)


_STARTING_TASK_RE = re.compile(
    r"##\s*Starting\s+Task[^\n]*\n+\s*(\S[^\n]*)", re.IGNORECASE
)
# How many of the JSONL's leading user messages SessionScan keeps for
# divergence-text fallback. Enough to find a non-inherited prompt
# without bloating the per-scan memory footprint.
_USER_PREFIX_CAP = 10
from .pricing import cost_usd as _cost_usd
from .status import Status, from_raw as _from_raw_status, merge as _merge_status


# --- Data classes -------------------------------------------------------


_TOKEN_KEYS = ("cw", "cr", "r", "w")


def _zero_tokens() -> dict[str, int]:
    """Empty integer-token counters keyed by ``cw/cr/r/w``."""
    return {k: 0 for k in _TOKEN_KEYS}


def _zero_costs() -> dict[str, float]:
    """Empty float-USD counters keyed by ``cw/cr/r/w``."""
    return {k: 0.0 for k in _TOKEN_KEYS}


def _add_into(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """``dst[k] += src[k]`` for every ``cw/cr/r/w`` key. Used to fold
    one window's counters into a subtree aggregate."""
    for k in _TOKEN_KEYS:
        dst[k] += src[k]


class UsageEvent(NamedTuple):
    """One assistant turn's ``message.usage`` counters + the model that
    produced them. Fields mirror Anthropic's wire format:
    ``cw=cache_creation_input_tokens``, ``cr=cache_read_input_tokens``,
    ``r=input_tokens``, ``w=output_tokens``.
    """

    ts: Optional[datetime]
    model: Optional[str]
    cw: int
    cr: int
    r: int
    w: int


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
    # True iff the LAST callstack envelope seen in an assistant message
    # was a ``{"op":"return"}``. Used to override a stale callstack
    # ``report.yaml`` status of ``running`` for a child whose JSONL
    # shows it already returned (the runtime sometimes fails to update
    # the report). Earlier returns followed by a later yield/run flip
    # this back to False — the LAST envelope is the terminal state.
    has_returned: bool = False
    # Persistent terminal-envelope tracking (NEVER reset on intervening
    # events). ``last_envelope_kind`` is the kind of the LAST callstack
    # envelope ever seen ("return" | "yield" | None), and
    # ``last_envelope_ts`` is its timestamp. Used by ``SpawnResolver`` to
    # infer fork-spawn status without re-walking the JSONL — a yield
    # followed by a resume followed by a return surfaces as ``complete``
    # at the latest envelope, because that's the terminal state.
    last_envelope_kind: Optional[str] = None
    last_envelope_ts: Optional[datetime] = None
    # For fork-detected sessions: the assigned task label captured from
    # the FIRST ``queue-operation`` record whose content matches
    # ``## Starting Task ... /task-X``. This is the callstack runtime's
    # primary divergence signal — when present, ForkDetector returns it
    # verbatim as the spawn label.
    queue_op_starting_task: Optional[str] = None
    # Fallback divergence source: the first few ``user``-record (uuid,
    # text) pairs in the JSONL. ForkDetector filters these against the
    # family root's uuid set to find the first message that ISN'T
    # inherited from the parent (i.e. the divergent prompt). Capped so
    # that long sessions don't bloat the cache.
    first_user_texts: list[tuple[str, str]] = field(default_factory=list)
    # Per-assistant-message token usage events. ``model`` is the
    # assistant message's ``message.model`` string, kept per-event so
    # cost can be priced at the rate of whichever model that specific
    # turn ran against (and so the attribution pass doesn't need a
    # separate cost array shadowing this one).
    usage_events: list["UsageEvent"] = field(default_factory=list)


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
    status: str  # ``done`` | ``live`` | ``yield`` — this window's own state
    # Max status across this window AND every descendant, with priority
    # ``live`` > ``yield`` > ``done``. Lets ancestors visually reflect
    # "work is still happening somewhere below" even when their own
    # turn has long since ended. Filled in post-order by
    # ``_aggregate_subtree_status``.
    subtree_status: str = "done"
    kind: str = ""    # ``root`` | ``call`` | ``subagent`` | ``resume``
    parent_window_id: Optional[str] = None
    children: list["WindowNode"] = field(default_factory=list)
    # Index of this window within its session (0-based, chronological).
    # Useful for the frontend to label "1st", "2nd" instances cleanly.
    window_index: int = 0
    # Token + USD counters keyed by ``cw/cr/r/w`` (see UsageEvent).
    # ``self_*`` is this window's own ``message.usage`` events; ``subtree_*``
    # adds every descendant. The root card renders a third $ footer row
    # from ``subtree_cost``; leaves skip the subtree row (it equals self).
    self_usage: dict[str, int] = field(default_factory=_zero_tokens)
    subtree_usage: dict[str, int] = field(default_factory=_zero_tokens)
    self_cost: dict[str, float] = field(default_factory=_zero_costs)
    subtree_cost: dict[str, float] = field(default_factory=_zero_costs)

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
            "subtree_status": self.subtree_status,
            "kind": self.kind,
            "parent_window_id": self.parent_window_id,
            "window_index": self.window_index,
            "self_usage": self.self_usage,
            "subtree_usage": self.subtree_usage,
            "self_cost": self.self_cost,
            "subtree_cost": self.subtree_cost,
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
    # Two derived flags driven by the same stream:
    #   at_user_prompt: last meaningful event = yield envelope OR stop_hook
    #   has_returned:   last assistant envelope = return
    # Both reset together on any "real" event (assistant turn without an
    # envelope, real user reply). Tool_result user records and unrelated
    # system subtypes don't count as events — they leave state alone.
    at_user_prompt = False
    has_returned = False
    for rec in iter_lines(path):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            if scan.start_ts is None:
                scan.start_ts = ts
            scan.end_ts = ts
        rtype = rec.get("type")
        if rtype == "assistant":
            msg = rec.get("message")
            if isinstance(msg, dict):
                u = msg.get("usage")
                if isinstance(u, dict):
                    cw = int(u.get("cache_creation_input_tokens") or 0)
                    cr = int(u.get("cache_read_input_tokens") or 0)
                    r_in = int(u.get("input_tokens") or 0)
                    w_out = int(u.get("output_tokens") or 0)
                    if cw or cr or r_in or w_out:
                        m = msg.get("model")
                        model = m if isinstance(m, str) else None
                        scan.usage_events.append(
                            UsageEvent(ts, model, cw, cr, r_in, w_out)
                        )
            text = _extract_assistant_text(rec)
            if text and _YIELD_RE.search(text):
                if ts is not None:
                    scan.yields.append(ts)
                at_user_prompt, has_returned = True, False
                scan.last_envelope_kind = "yield"
                scan.last_envelope_ts = ts
            elif text and _RETURN_RE.search(text):
                at_user_prompt, has_returned = False, True
                scan.last_envelope_kind = "return"
                scan.last_envelope_ts = ts
            else:
                at_user_prompt, has_returned = False, False
        elif rtype == "user" and not _is_tool_result_record(rec):
            # Tool results leave state alone (mid-turn tool processing);
            # real user replies reset both flags.
            at_user_prompt, has_returned = False, False
            u = rec.get("uuid")
            if (
                isinstance(u, str)
                and len(scan.first_user_texts) < _USER_PREFIX_CAP
            ):
                scan.first_user_texts.append(
                    (u, _text_blocks(rec.get("message"), " ") or "")
                )
        elif rtype == "queue-operation":
            if scan.queue_op_starting_task is None:
                content = rec.get("content")
                if isinstance(content, str):
                    m = _STARTING_TASK_RE.search(content)
                    if m:
                        scan.queue_op_starting_task = m.group(1).strip()
        elif rtype == "system" and rec.get("subtype") == "stop_hook_summary":
            # End-of-turn marker. Sets at_user_prompt but doesn't touch
            # has_returned — a stop hook after a return envelope mustn't
            # un-flag the return.
            at_user_prompt = True
    scan.at_user_prompt = at_user_prompt
    scan.has_returned = has_returned
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






# --- Invocation enumeration --------------------------------------------


def collect_invocations(
    callstack_index: Any = None,
    subagent_index: Any = None,
    *,
    known_session_ids: Optional[set[str]] = None,
    spawn_resolver: Any = None,
    fork_detector: Any = None,
) -> dict[str, list[Invocation]]:
    """Enumerate every parent → child invocation, keyed by child session id.

    Sources are unified by :class:`unwind.spawns.SpawnResolver` — one
    place that knows about callstack reports, the fork detector (for
    in-flight forks before ``report.yaml`` lands), and the subagent
    index. Pass ``spawn_resolver`` for new code; the legacy
    ``callstack_index`` / ``subagent_index`` / ``fork_detector`` kwargs
    are accepted (composing an ad-hoc resolver) so existing tests still
    work.

    Each list is sorted chronologically by ``started_at`` so the K-th
    entry corresponds to the K-th window of the child.

    ``known_session_ids`` is no longer used (the resolver enumerates
    subagent parents itself); kept as a keyword for backward
    compatibility with the previous signature.
    """
    del known_session_ids  # unused; kept for signature compatibility

    if spawn_resolver is None:
        from .spawns import SpawnResolver
        from .callstack import CallstackIndex
        from .fork_detect import ForkDetector
        from .subagents import SubagentIndex

        sentinel = Path("/dev/null/no-data")
        cs = callstack_index or CallstackIndex(sentinel)
        fd = fork_detector or ForkDetector(sentinel)
        sa = subagent_index or SubagentIndex(sentinel)
        proj_dir = (
            getattr(fd, "_project_dir", None)
            or getattr(sa, "_project_dir", None)
            or sentinel
        )
        spawn_resolver = SpawnResolver(cs, fd, sa, project_dir=proj_dir)

    out: dict[str, list[Invocation]] = {}
    for parent_sid, spawns in spawn_resolver.spawns_by_parent().items():
        for s in spawns:
            out.setdefault(s.child_session_id, []).append(
                Invocation(
                    caller_session_id=parent_sid,
                    target_session_id=s.child_session_id,
                    started_at=s.started_at,
                    ended_at=s.ended_at,
                    label=s.label or s.child_session_id[:8],
                    status=s.status,
                    kind=s.kind,
                    invoke_id=s.invoke_id or "",
                )
            )

    for invs in out.values():
        invs.sort(key=lambda i: i.started_at or EPOCH)
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
        # mirrors the invocation's task status (via the canonical
        # translator) but gates ``live`` on actual process liveness.
        canonical = _from_raw_status(inv.status) or "done"
        is_last = k == n - 1
        if not is_last:
            status: Status = "done"
        elif canonical == "yield":
            status = "yield"
        elif canonical == "live":
            status = "live" if is_live else "done"
        elif canonical == "failed":
            status = "failed"
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


def _attribute_self_usage(
    windows: list[WindowNode],
    usage_events: list[UsageEvent],
) -> None:
    """Sum each per-record event into the window whose
    ``[window_start, window_end)`` contains ``ts``, and price it at the
    rate of the recording model.

    Events with no timestamp attribute to the first window (same rule
    ``_find_window_for_ts`` uses for ``ts is None``); events past the
    last window's end attribute to the last window so late bookkeeping
    still gets counted somewhere.
    """
    if not windows or not usage_events:
        return
    for ev in usage_events:
        w = _find_window_for_ts(windows, ev.ts)
        if w is None:
            continue
        _add_into(w.self_usage, ev._asdict())
        _add_into(w.self_cost, _cost_usd(ev.model, ev.cw, ev.cr, ev.r, ev.w))


def _aggregate_subtree_status(
    node: WindowNode, _seen: Optional[set[str]] = None
) -> Status:
    """Post-order walk: each node's ``subtree_status`` = the highest-priority
    status across the node itself and every descendant.

    Delegates priority to :func:`unwind.status.merge` — ``live > yield >
    failed > done``. A single live descendant pulls an otherwise-finished
    ancestor's rail back into ``live`` so the UI signals that work is
    still happening somewhere below.

    The ``_seen`` set defends against accidental cycles in the wiring
    pass (a window grafted under two parents). Without it, the recursion
    would loop forever; with it, the second visit returns ``done`` so
    aggregation stays bounded.
    """
    if _seen is None:
        _seen = set()
    if node.window_id in _seen:
        return "done"
    _seen.add(node.window_id)
    own = _from_raw_status(node.status) or "done"
    child_statuses: list[Optional[Status]] = [
        _aggregate_subtree_status(c, _seen) for c in node.children
    ]
    merged = _merge_status([own, *child_statuses])
    node.subtree_status = merged
    return merged


def _aggregate_subtree_usage(node: WindowNode) -> tuple[dict[str, int], dict[str, float]]:
    """Post-order walk: each parent's ``subtree_*`` = its own ``self_*``
    plus every descendant's. Children are visited before the parent so
    leaves are settled first and the totals bubble up toward the root.
    Returns ``(subtree_usage, subtree_cost)``.
    """
    usage = dict(node.self_usage)
    cost = dict(node.self_cost)
    for child in node.children:
        c_usage, c_cost = _aggregate_subtree_usage(child)
        _add_into(usage, c_usage)
        _add_into(cost, c_cost)
    node.subtree_usage = usage
    node.subtree_cost = cost
    return usage, cost


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
    callstack_index: Any = None,
    *,
    subagent_index: Any = None,
    fork_detector: Any = None,
    spawn_resolver: Any = None,
    builder: Optional["CanvasTreeBuilder"] = None,
    is_live_session: IsLiveFn = lambda _sid: False,
    title_for: TitleFn = lambda _sid: None,
) -> tuple[WindowNode, list[WindowNode]]:
    """Compute the canvas tree rooted at ``root_session_id``.

    Pass ``spawn_resolver`` (preferred) — a unified view over callstack
    reports + fork detector + subagent index. The legacy individual
    indexes are accepted for backwards compatibility (an ad-hoc
    resolver is composed from them).

    Returns ``(root_window, all_windows_flat)``.
    """
    invocations_by_target = collect_invocations(
        callstack_index,
        subagent_index,
        spawn_resolver=spawn_resolver,
        fork_detector=fork_detector,
    )

    # BFS from root over the parent → child edges to discover every
    # session reachable from this canvas.
    calls_from: dict[str, set[str]] = {}
    for target_sid, invs in invocations_by_target.items():
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
        ws = _compute_windows(
            scan,
            invs,
            is_root=is_root,
            is_live=is_live_session(sid),
            title=title,
        )
        _attribute_self_usage(ws, scan.usage_events)
        windows_by_session[sid] = ws

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

    # Sort each window's children to MATCH the parent's CALL row order
    # in the compact card. Without this the canvas columns end up
    # alphabetical-by-sid within each timestamp group while the rows are
    # in requested-tasks order — and the connectors cross.
    #
    # Strategy: read each parent's JSONL once, ask the resolver for the
    # display order (mirrors derive-rows.ts logic), and use it as the
    # primary sort key. Fall back to ``window_start`` for parents
    # without a JSONL (subagent leaves) or when the resolver isn't
    # plumbed through.
    display_order_cache: dict[str, dict[str, int]] = {}

    def _display_order_for(parent_sid: str) -> dict[str, int]:
        if parent_sid in display_order_cache:
            return display_order_cache[parent_sid]
        order: dict[str, int] = {}
        if spawn_resolver is not None:
            path = project_dir / f"{parent_sid}.jsonl"
            if path.is_file():
                try:
                    from .messages import read_messages

                    page = read_messages(path)
                    order = spawn_resolver.child_display_order(
                        parent_sid, page.messages
                    )
                except Exception:
                    order = {}
        display_order_cache[parent_sid] = order
        return order

    for w in all_windows:
        if not w.children:
            continue
        order = _display_order_for(w.session_id)
        w.children.sort(
            key=lambda c: (
                order.get(c.session_id, 10**9),
                c.window_start or EPOCH,
            )
        )

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
        _aggregate_subtree_usage(root)
        _aggregate_subtree_status(root)
        return root, all_windows
    root = root_windows[0]
    # Post-order subtree token totals — must run AFTER children are wired
    # in above so the recursion sees the full descendant set.
    _aggregate_subtree_usage(root)
    _aggregate_subtree_status(root)
    return root, all_windows


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
        self._cache = PathCache(scan_session)

    @property
    def project_dir(self) -> Path:
        return self._project_dir

    def get_scan(self, session_id: str) -> SessionScan:
        path = self._project_dir / f"{session_id}.jsonl"
        return self._cache.get(path)
