"""Canvas tree builder: assemble per-session :class:`SessionScan` results
into the window-tree that the frontend renders.

A *window* is a slice of one session's activity bounded by parent
invocations. The parent calls the child K times (initial + K-1
resumes); the child has K windows, one per invocation. Each parent
window is the slice of the parent that contains a particular
invocation timestamp.

This module owns assembly only. The single-pass JSONL parser that
feeds it lives in :mod:`unwind.session_scan` (re-exported below as
:class:`SessionScan` / :func:`scan_session` / :class:`CanvasTreeBuilder`
for legacy import paths). The high-level flow:

* enumerate all parent → child invocations via :class:`SpawnResolver`
* pull each reachable session's cached :class:`SessionScan`
* assign the K-th invocation to the K-th window of the target
* find the parent's containing window by timestamp
* finalise the tree (status + token usage + USD cost) in one post-order
  walk via :func:`_finalize_subtree`

The result is a single immutable tree the frontend renders directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from .spawns import SpawnResolver

from .jsonl import EPOCH
from .pricing import cost_usd as _cost_usd
from .session_scan import (
    CanvasTreeBuilder,
    SessionScan,
    UsageEvent,
    scan_session,
)
from .status import Status, from_raw as _from_raw_status, merge as _merge_status
from .subagents import SUBAGENT_PREFIX


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


# Re-exports so external callers don't have to learn the split. The
# canonical home for these is :mod:`unwind.session_scan`.
__all__ = [
    "CanvasTreeBuilder",
    "Invocation",
    "SessionScan",
    "UsageEvent",
    "WindowNode",
    "build_canvas_tree",
    "collect_invocations",
    "scan_session",
]


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
    # For workflow spawns: ``run`` | ``phase`` | ``agent`` — drives the
    # window kind in the wiring step. ``None`` for callstack/fork/subagent.
    node_role: Optional[str] = None


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
    # Extra parent → child edges sourced from ``await_call`` tool_uses in
    # THIS window's session. An ``await_call`` references an already-
    # running invocation by ``invoke_id`` instead of spawning a new
    # child; the canvas still needs an edge from the await row's right-
    # side handle to the original child window so the relationship is
    # visible. The standard ``parent_window_id`` edge (one per child
    # window) only anchors to the originating ``call`` row's handle —
    # without these, the await row's handle has no incoming edge and
    # the connection looks broken. Each entry:
    #   ``parent_tool_use_id`` — the await_call tool_use's id; the
    #     frontend assembles the source handle id from it.
    #   ``target_window_id``  — the child window the await refers to
    #     (resolved by matching the await's ``invoke_id`` against the
    #     ``Invocation`` chain).
    follower_edges: list[dict[str, str]] = field(default_factory=list)

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
            "follower_edges": list(self.follower_edges),
            "children": [c.to_dict() for c in self.children],
        }




# --- Invocation enumeration --------------------------------------------


def collect_invocations(
    spawn_resolver: "SpawnResolver",
) -> dict[str, list[Invocation]]:
    """Enumerate every parent → child invocation, keyed by child session id.

    Each list is sorted chronologically by ``started_at`` so the K-th
    entry corresponds to the K-th window of the child.
    """
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
                    invoke_id=getattr(s, "invoke_id", "") or "",
                    node_role=getattr(s, "node_role", None),
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
        # Single-window case: no successor window to tile against, so the
        # half-open ``[start, end)`` filter has no job to do — but it can
        # still drop a message whose timestamp equals ``scan.end_ts``
        # (notably the last tool_use, which is the spawning anchor for a
        # still-running child). Leave the window open-ended so the closing
        # message lands inside and the child's edge has a handle to anchor.
        end = None
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
    inherited_uuids: Optional[set[str]] = None,
) -> None:
    """Sum each per-record event into the window whose
    ``[window_start, window_end)`` contains ``ts``, and price it at the
    rate of the recording model.

    Events with no timestamp attribute to the first window (same rule
    ``_find_window_for_ts`` uses for ``ts is None``); events past the
    last window's end attribute to the last window so late bookkeeping
    still gets counted somewhere.

    ``inherited_uuids`` is the set of uuids this session inherited from
    its callstack ancestors (``--fork-session`` mirrors the parent's
    transcript, including each assistant turn's ``message.usage``).
    Events with a matching uuid are skipped so the fork's window
    doesn't double-count tokens the parent already paid for.
    """
    if not windows or not usage_events:
        return
    inh = inherited_uuids or ()
    for ev in usage_events:
        if ev.uuid is not None and ev.uuid in inh:
            continue
        w = _find_window_for_ts(windows, ev.ts)
        if w is None:
            continue
        # Build the token-dict explicitly: ``_add_into`` only iterates
        # ``_TOKEN_KEYS`` (cw/cr/r/w), but ``ev._asdict()`` also includes
        # ``ts`` / ``model``. The current loop happens to work because
        # ``_TOKEN_KEYS`` is a subset; an explicit dict keeps that
        # invariant from quietly breaking if either side changes.
        _add_into(w.self_usage, {"cw": ev.cw, "cr": ev.cr, "r": ev.r, "w": ev.w})
        _add_into(w.self_cost, _cost_usd(ev.model, ev.cw, ev.cr, ev.r, ev.w))


def _finalize_subtree(
    node: WindowNode, _seen: Optional[set[str]] = None
) -> tuple[Status, dict[str, int], dict[str, float]]:
    """Post-order walk: roll status + token usage + USD cost up the tree
    in a single pass.

    Each parent's ``subtree_status`` is the highest-priority status across
    itself and every descendant (priority via :func:`unwind.status.merge`
    — ``live > yield > failed > done``). A single live descendant pulls
    an otherwise-finished ancestor's rail back into ``live`` so the UI
    signals that work is still happening somewhere below.

    Note this walk does NOT apply the terminal-ancestor wall that
    :meth:`CallstackIndex.aggregate_status_for_session` uses. It doesn't
    need to: each window's ``status`` was already gated on real process
    liveness in ``_compute_windows`` (a ``running`` report with no live
    process is downgraded to ``done``), so a ``live`` descendant here is
    genuinely live and SHOULD escalate — even above a finished ancestor.
    The wall guards the pure-report path, which has no process cross-check.

    Each parent's ``subtree_usage`` / ``subtree_cost`` are its own
    ``self_*`` plus every descendant's, summed element-wise.

    The ``_seen`` set defends against accidental cycles in the wiring
    pass (a window grafted under two parents). On a second visit, status
    short-circuits to ``done`` and the usage/cost contribution is zero
    so aggregation stays bounded across all three fields.
    """
    if _seen is None:
        _seen = set()
    if node.window_id in _seen:
        return "done", _zero_tokens(), _zero_costs()
    _seen.add(node.window_id)
    own_status = _from_raw_status(node.status) or "done"
    statuses: list[Optional[Status]] = [own_status]
    usage = dict(node.self_usage)
    cost = dict(node.self_cost)
    for child in node.children:
        c_status, c_usage, c_cost = _finalize_subtree(child, _seen)
        statuses.append(c_status)
        _add_into(usage, c_usage)
        _add_into(cost, c_cost)
    node.subtree_status = _merge_status(statuses)
    node.subtree_usage = usage
    node.subtree_cost = cost
    return node.subtree_status, usage, cost


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
    *,
    spawn_resolver: "SpawnResolver",
    builder: Optional["CanvasTreeBuilder"] = None,
    is_live_session: IsLiveFn = lambda _sid: False,
    title_for: TitleFn = lambda _sid: None,
) -> tuple[WindowNode, list[WindowNode]]:
    """Compute the canvas tree rooted at ``root_session_id``.

    ``spawn_resolver`` is mandatory — it unifies the callstack reports,
    fork detector, and subagent index views into one Spawn iterator.

    Returns ``(root_window, all_windows_flat)``.
    """
    invocations_by_target = collect_invocations(spawn_resolver)

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

    def _scan_for(sid: str) -> SessionScan:
        """Scan ``sid``'s transcript. Subagent synthetic ids
        (``agent-<id>``) live at ``<parent>/subagents/agent-<id>.jsonl``,
        resolved via the spawn resolver, so their ``message.usage`` is
        counted — a flat ``project_dir/<sid>.jsonl`` would not exist and
        the subagent's tokens would go uncounted."""
        if sid.startswith(SUBAGENT_PREFIX):
            sub_path = spawn_resolver.subagent_jsonl_path(sid)
            if sub_path is not None:
                return (
                    builder.get_scan_at(sub_path)
                    if builder is not None
                    else scan_session(sub_path)
                )
            return SessionScan(session_id=sid, path=project_dir / f"{sid}.jsonl")
        if builder is not None:
            return builder.get_scan(sid)
        path = project_dir / f"{sid}.jsonl"
        return scan_session(path) if path.is_file() else SessionScan(
            session_id=sid, path=path
        )

    # Scan every reachable session, plus any orphan invocation targets
    # whose caller isn't reachable from the root.
    scans: dict[str, SessionScan] = {}
    for sid in real_sessions:
        scans[sid] = _scan_for(sid)
    visited = set(real_sessions)
    for target_sid in invocations_by_target:
        if target_sid in visited:
            continue
        visited.add(target_sid)
        scans[target_sid] = _scan_for(target_sid)

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
        # Inherited uuids from the parent chain — events with these
        # uuids were copied into this fork's JSONL by
        # ``--fork-session`` and represent work the parent already
        # booked. Root sessions have no ancestors; skip the lookup.
        inherited = (
            set() if is_root else spawn_resolver.inherited_uuids_for(sid)
        )
        _attribute_self_usage(ws, scan.usage_events, inherited_uuids=inherited)
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
            #   * workflow run/phase nodes get their own kinds; workflow
            #     agents reuse "subagent" (real transcript + leaf rendering);
            #   * subagent invocations stay as "subagent";
            #   * the first callstack invocation = "call";
            #   * subsequent callstack invocations = "resume".
            if inv.node_role == "run":
                tw.kind = "workflow"
            elif inv.node_role == "phase":
                tw.kind = "workflow_phase"
            elif inv.node_role == "agent":
                tw.kind = "subagent"
            elif inv.kind != "subagent":
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
    # Per-parent cached read of the parent's annotated message stream.
    # Two consumers below share the same read: ``child_display_order``
    # (for child sort) and follower-edge extraction (for await_call →
    # already-running-invocation edges that the standard parent_window
    # wiring doesn't cover).
    parent_msgs_cache: dict[str, Optional[list[Any]]] = {}

    def _annotated_messages_for(parent_sid: str) -> Optional[list[Any]]:
        if parent_sid in parent_msgs_cache:
            return parent_msgs_cache[parent_sid]
        path = project_dir / f"{parent_sid}.jsonl"
        msgs: Optional[list[Any]] = None
        if path.is_file():
            from .messages import annotate_spawns, read_messages

            try:
                page = read_messages(path)
            except OSError:
                page = None
            if page is not None:
                # Mutates each tool_use to set ``spawn_is_follower`` /
                # ``spawn_session_ids``; we read those flags below.
                annotate_spawns(
                    page.messages,
                    current_session_id=parent_sid,
                    spawn_resolver=spawn_resolver,
                )
                msgs = page.messages
        parent_msgs_cache[parent_sid] = msgs
        return msgs

    def _display_order_for(parent_sid: str) -> dict[str, int]:
        msgs = _annotated_messages_for(parent_sid)
        if msgs is None:
            return {}
        return spawn_resolver.child_display_order(parent_sid, msgs)

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

    # Follower-edge population: for every window whose session emits
    # ``await_call`` tool_uses, resolve each await's invoke_id to the
    # specific child window it polls (K-th invocation → K-th window of
    # the target session) and attach a ``follower_edge`` so the
    # frontend can draw an edge from the await row's handle to that
    # window. Done after children are wired so we have a stable
    # ``windows_by_session`` to look up targets in.
    for w in all_windows:
        msgs = _annotated_messages_for(w.session_id)
        if not msgs:
            continue
        for m in msgs:
            if getattr(m, "role", None) != "tool_use":
                continue
            if not getattr(m, "spawn_is_follower", False):
                continue
            tu_id = getattr(m, "tool_use_id", None)
            child_sids = getattr(m, "spawn_session_ids", None) or []
            if not tu_id or not child_sids:
                continue
            target_sid = child_sids[0]
            # Pick the specific window: match by invoke_id when we can
            # (yield/resume chains have multiple windows for one sid);
            # fall back to the first window otherwise.
            target_win = _resolve_follower_target_window(
                m, target_sid, invocations_by_target, windows_by_session
            )
            if target_win is None:
                continue
            w.follower_edges.append(
                {
                    "parent_tool_use_id": tu_id,
                    "target_window_id": target_win.window_id,
                }
            )

    # Root window is index 0 of root_session_id (root sessions always
    # get a single window — see _compute_windows). When the root session
    # isn't even on disk (e.g. an in-flight subagent canvas), fabricate a
    # placeholder so the tree still has a root to anchor descendants to.
    root_windows = windows_by_session.get(root_session_id) or []
    if root_windows:
        root = root_windows[0]
    else:
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

    # Post-order subtree finalisation: status + token usage + USD cost
    # in one walk. Must run AFTER children are wired in above so the
    # recursion sees the full descendant set.
    _finalize_subtree(root)
    return root, all_windows


def _resolve_follower_target_window(
    follower_msg: Any,
    target_sid: str,
    invocations_by_target: dict[str, list["Invocation"]],
    windows_by_session: dict[str, list[WindowNode]],
) -> Optional[WindowNode]:
    """Pick the child window an ``await_call`` follower row points at.

    A child session has K windows (one per invocation: initial + K-1
    resumes). The await refers to the specific invocation matching its
    ``invoke_id``. Match by invoke_id when possible; fall back to the
    first window when the message doesn't carry a usable invoke_id
    (the K=1 case, which is the overwhelming majority).
    """
    target_windows = windows_by_session.get(target_sid) or []
    if not target_windows:
        return None
    # Prefer the exact invocation match when the message carries an
    # invoke_id (tool_input.invoke_id or echoed in tool_result).
    from .messages import _invoke_id_for_await
    invoke_id = _invoke_id_for_await(follower_msg)
    if invoke_id:
        invs = invocations_by_target.get(target_sid) or []
        for k, inv in enumerate(invs):
            if inv.invoke_id == invoke_id and k < len(target_windows):
                return target_windows[k]
    return target_windows[0]


def _short_session_label(sid: str) -> str:
    # ``agent-<id>`` synthetic subagent ids: drop the prefix.
    if sid.startswith("agent-"):
        return sid[6:14]
    return sid[:8]


# CanvasTreeBuilder lives in unwind.session_scan (re-exported above).
