"""Read callstack plugin logs to build cross-session call trees.

The plugin writes ``<project>/.claude/callstack/log/<invoke_id>/report.yaml``
at the end of each ``invoke_parallel`` / ``invoke``. That YAML already carries
the complete recursive tree of tasks spawned by that invocation, so we just
merge it across invocations.

Algorithm (see PRD §7.2 and PLAN Phase 3):

1. Enumerate every ``report.yaml`` under the callstack log dir.
2. Group by ``parent_session`` → list of reports.
3. ``build_tree(root)`` = for each report where parent == root, graft its
   ``tasks`` (with nested ``children`` intact) as direct children of root.
   Recurse into child sessions to pick up separate invocations they made.

Stale reports (status = running, but the plugin crashed) are included with
their last-known status; the UI displays them as in-progress.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime  # noqa: F401
from pathlib import Path
from typing import Any, Optional

from ._cache import PathCache
from .jsonl import EPOCH, parse_ts as _parse_ts

import yaml

from .status import Status, from_raw as _from_raw_status, merge as _merge_status


@dataclass
class TaskNode:
    session_id: Optional[str]
    task: str
    status: str
    depth: int
    duration_seconds: Optional[float]
    summary: Optional[str]
    error: Optional[str]
    children: list["TaskNode"] = field(default_factory=list)
    invoke_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    # ``call`` for callstack forks, ``subagent`` for Agent-tool subagents.
    kind: str = "call"
    # Sub-classification within ``kind == "call"``: how the underlying claude
    # session was launched. Read from report.yaml; defaults to ``"fork"`` for
    # backward compat with reports written before this field existed.
    #   "fork"                — `--resume <parent> --fork-session`
    #   "fresh"               — brand-new session in the parent's project
    #   "fresh_cross_project" — brand-new session in a different project
    # Always ``"fork"`` for ``kind == "subagent"`` (the field is meaningful
    # only for callstack spawns).
    call_type: str = "fork"

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "task": self.task,
            "status": self.status,
            "depth": self.depth,
            "duration_seconds": self.duration_seconds,
            "summary": self.summary,
            "error": self.error,
            "invoke_id": self.invoke_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
            "kind": self.kind,
            "call_type": self.call_type,
            "children": [c.to_dict() for c in self.children],
        }


@dataclass
class InvokeReport:
    invoke_id: str
    parent_session: str
    started_at: Optional[datetime]
    ended_at: Optional[datetime]
    status: str
    tasks: list[TaskNode]
    path: Path


class CallstackIndex:
    """Caches parsed reports keyed by ``invoke_id``."""

    def __init__(self, log_dir: Path) -> None:
        self._log_dir = log_dir
        self._cache = PathCache(self._load_report)
        # Memoize aggregate views (_latest_view / reports_by_parent) by a
        # cheap signature over report.yaml stats. Without this, a single
        # /sessions response triggers ~2 full rebuilds per row (~hundreds
        # of full tree walks for a project with 200 sessions × 50 reports).
        self._view_lock = threading.Lock()
        self._view_sig: Optional[tuple] = None
        self._view_cached: Optional[tuple[dict[str, TaskNode], dict[str, list[str]], dict[str, str]]] = None
        self._rbp_sig: Optional[tuple] = None
        self._rbp_cached: Optional[dict[str, list[InvokeReport]]] = None

    @property
    def log_dir(self) -> Path:
        return self._log_dir

    @property
    def has_logs(self) -> bool:
        return self._log_dir.is_dir()

    def all_reports(self) -> list[InvokeReport]:
        if not self.has_logs:
            return []
        out: list[InvokeReport] = []
        for invoke_dir in self._log_dir.iterdir():
            if not invoke_dir.is_dir():
                continue
            report_path = invoke_dir / "report.yaml"
            if not report_path.is_file():
                continue
            rep = self._cache.get(report_path)
            if rep is not None:
                out.append(rep)
        return out

    def _log_signature(self) -> tuple:
        """Cheap fingerprint over all report.yaml files in the log dir.

        Used to invalidate the memoized aggregate views. Cost: one stat per
        report, vs the O(reports × tree-size) recursion in ``_latest_view``.
        """
        if not self.has_logs:
            return ()
        sig: list[tuple[str, float, int]] = []
        try:
            for invoke_dir in self._log_dir.iterdir():
                if not invoke_dir.is_dir():
                    continue
                report_path = invoke_dir / "report.yaml"
                try:
                    st = report_path.stat()
                except OSError:
                    continue
                sig.append((invoke_dir.name, st.st_mtime, st.st_size))
        except OSError:
            return ()
        sig.sort()
        return tuple(sig)

    def reports_by_parent(self) -> dict[str, list[InvokeReport]]:
        sig = self._log_signature()
        with self._view_lock:
            if self._rbp_cached is not None and self._rbp_sig == sig:
                return self._rbp_cached
        by_parent: dict[str, list[InvokeReport]] = {}
        for rep in self.all_reports():
            by_parent.setdefault(rep.parent_session, []).append(rep)
        with self._view_lock:
            self._rbp_cached = by_parent
            self._rbp_sig = sig
        return by_parent

    def _latest_view(
        self,
    ) -> tuple[
        dict[str, TaskNode],
        dict[str, list[str]],
        dict[str, str],
    ]:
        """Build a ``latest report wins`` view of all sessions ever recorded.

        Each call to ``invoke``/``invoke_parallel`` writes its own
        ``report.yaml``. Re-running the same workflow produces multiple
        reports that describe the same chain at different points in time —
        e.g. an old report says ``yielded`` while the latest says
        ``complete``. Walking every report indiscriminately resurrects the
        old ``yielded``; deduping by ``session_id`` and keeping the entry
        from the most recent report yields the right current snapshot.

        Returns:
          - ``canonical``: ``{session_id: TaskNode}`` — the TaskNode from the
            most-recent report that mentioned this session.
          - ``children_sids``: ``{session_id: [child_sid, ...]}`` — direct
            children, deduped (a sid appears once in insertion order across
            reports) so each child renders as exactly one row even when
            multiple reports record the same parent→child edge.
          - ``root_status``: ``{parent_session: report_status}`` — the
            report-level status from the most recent report whose
            ``parent_session`` is this id (i.e. the status visible to a
            "root caller" that itself never appears as a task).

        Memoized by ``_log_signature`` so a /sessions response with N rows
        does ~1 rebuild instead of ~2N.
        """
        sig = self._log_signature()
        with self._view_lock:
            if self._view_cached is not None and self._view_sig == sig:
                return self._view_cached
        canonical: dict[str, TaskNode] = {}
        canonical_ts: dict[str, Optional[datetime]] = {}
        children_sids: dict[str, list[str]] = {}
        children_seen: dict[str, set[str]] = {}
        root_status: dict[str, str] = {}
        root_status_ts: dict[str, Optional[datetime]] = {}

        def newer(a: Optional[datetime], b: Optional[datetime]) -> bool:
            return (a or EPOCH) > (b or EPOCH)

        def absorb(node: TaskNode, ts: Optional[datetime]) -> None:
            sid = node.session_id
            if sid:
                prev_ts = canonical_ts.get(sid)
                if prev_ts is None or newer(ts, prev_ts):
                    canonical[sid] = node
                    canonical_ts[sid] = ts
                seen = children_seen.setdefault(sid, set())
                order = children_sids.setdefault(sid, [])
                for c in node.children:
                    csid = c.session_id
                    if csid and csid not in seen:
                        seen.add(csid)
                        order.append(csid)
            for c in node.children:
                absorb(c, ts)

        for rep in self.all_reports():
            ts = rep.started_at
            psid = rep.parent_session
            if psid:
                if rep.status:
                    prev_ts = root_status_ts.get(psid)
                    if prev_ts is None or newer(ts, prev_ts):
                        root_status[psid] = rep.status.lower()
                        root_status_ts[psid] = ts
                seen = children_seen.setdefault(psid, set())
                order = children_sids.setdefault(psid, [])
                for t in rep.tasks:
                    csid = t.session_id
                    if csid and csid not in seen:
                        seen.add(csid)
                        order.append(csid)
            for t in rep.tasks:
                absorb(t, ts)

        result = (canonical, children_sids, root_status)
        with self._view_lock:
            self._view_cached = result
            self._view_sig = sig
        return result

    def is_callstack_task(self, session_id: str) -> bool:
        """Whether ``session_id`` appears as a TaskNode in any report.

        A session can appear in callstack data in two distinct ways:

        1. **Task** — it's the ``session_id`` of a TaskNode somewhere in a
           report's task tree. Means callstack tracks this session's
           lifecycle precisely (it was spawned via ``invoke``/
           ``invoke_resume``).
        2. **Root only** — it's only ever the ``parent_session`` of reports
           it spawned, never a task. This is the user's main Claude Code
           session: it spawned callstack invocations, but callstack has no
           opinion on the session's own lifecycle. Status for these must
           come from process detection / JSONL mtime, not from
           ``aggregate_status_for_session`` (which would aggregate the
           descendants' statuses and falsely report ``done`` once all
           invocations terminate).
        """
        canonical, _, _ = self._latest_view()
        return session_id in canonical

    def aggregate_status_for_session(self, session_id: str) -> Optional["Status"]:
        """Return the canonical status for ``session_id`` and all descendants.

        Walks the latest-report view and merges the session's own status
        with every descendant's via :func:`unwind.status.merge`. Priority
        is ``live > yield > failed > done`` — a running descendant pulls
        an otherwise-yielded ancestor's status back to ``live`` so the
        UI signals that work is still happening.

        **Terminal-ancestor wall**: if the session's OWN status is already
        terminal (``failed`` or ``done``), we return it without walking
        descendants. A returned/failed invocation cannot have a genuinely
        live descendant — the runtime gates a call's return on its children
        returning first. When a child's ``report.yaml`` is left frozen at
        ``running`` (parent crashed, runtime died before writing the
        terminal envelope), that entry is stale debt, not live work, and
        must not resurrect the parent's status to ``live``. ``yield`` is
        deliberately NOT a wall: a yielded parent waiting on user input can
        legitimately sit above still-running descendants, so escalation
        still applies there. When the wall fires, descendants of ANY status
        — including ``yielded`` — are suppressed: a failed/done parent pins
        the verdict regardless of what its descendants report. See
        :mod:`unwind.status` for the invariant.

        The wall reads the session's OWN status from ``canonical[sid]``
        ONLY — the terminal verdict recorded by the caller that invoked
        this session. It deliberately does NOT consult ``root_status[sid]``,
        which carries the status of invocations this session itself
        *spawned*: an orchestrator whose latest sub-``/call`` report is
        still frozen at ``running`` would otherwise inject ``live`` into the
        own-status merge and bypass the wall (the original phase4 bug). A
        "main" session that only ever appears as a ``parent_session`` (never
        as a task) has no ``canonical`` entry, so the wall can't fire — it
        falls through to the descendant walk, as before.

        Returns ``None`` if this session doesn't appear in any report.
        Callers that need to distinguish "session is live (genuinely in
        flight)" from "session is waiting" should check the canonical
        ``"live"`` / ``"yield"`` directly.
        """
        canonical, children_sids, root_status = self._latest_view()
        if session_id not in canonical and session_id not in root_status:
            return None

        # Terminal-ancestor wall: the session's OWN terminal verdict
        # (canonical[sid], recorded by its caller) pins the result. See
        # the docstring for why root_status[sid] is excluded here.
        own_node = canonical.get(session_id)
        if own_node is not None:
            own = _from_raw_status(own_node.status)
            if own in ("failed", "done"):
                return own

        # Fall-through: own status is live / yield / unknown, or the session
        # only appears as a parent_session. Aggregate own + every descendant.
        statuses: list[Optional[Status]] = []
        if own_node is not None:
            statuses.append(_from_raw_status(own_node.status))
        if session_id in root_status:
            statuses.append(_from_raw_status(root_status[session_id]))
        visited: set[str] = {session_id}
        queue: list[str] = list(children_sids.get(session_id, []))
        while queue:
            sid = queue.pop()
            if sid in visited:
                continue
            visited.add(sid)
            node = canonical.get(sid)
            if node is not None:
                statuses.append(_from_raw_status(node.status))
            if sid in root_status:
                statuses.append(_from_raw_status(root_status[sid]))
            queue.extend(children_sids.get(sid, []))

        if not any(s is not None for s in statuses):
            return None
        return _merge_status(statuses)

    def direct_children_of(self, session_id: str) -> list["TaskNode"]:
        """Find this session's direct children across every report.

        Children are deduped by ``session_id`` via ``_latest_view`` — when
        the same parent→child edge is recorded in multiple reports (e.g.
        the workflow ran twice), each child appears exactly once and uses
        the most recent report's snapshot for its status.
        """
        canonical, children_sids, _ = self._latest_view()
        return [
            canonical[c]
            for c in children_sids.get(session_id, [])
            if c in canonical
        ]

    def direct_invocations_of(self, session_id: str) -> list["TaskNode"]:
        """Every direct child invocation of ``session_id`` across all reports.

        Unlike ``direct_children_of``, this does NOT deduplicate by
        ``session_id``. If three separate reports each record
        ``parent → child`` (e.g. the parent invoked the same child three
        times via callstack), this returns three TaskNodes — one per
        invocation — each carrying its own ``invoke_id`` and
        ``started_at``. The canvas uses the timestamps to bucket
        invocations into per-window child cards (mirroring how
        MCP-anchored spawns are partitioned via ``windowsForParent``).

        Returned in chronological order by ``started_at`` (TaskNodes
        without a timestamp sort to the front).
        """
        out: list[TaskNode] = []

        for rep in self.all_reports():
            # Top-level tasks: the report's own ``parent_session`` IS the
            # parent of every task in ``rep.tasks``.
            if rep.parent_session == session_id:
                out.extend(rep.tasks)
            # Nested children: find any TaskNode in this report whose
            # session_id == ``session_id`` and harvest its direct
            # children. Multiple matches in the same report are unusual
            # but handled (each contributes its children).

            def visit(node: TaskNode) -> None:
                if node.session_id == session_id:
                    out.extend(node.children)
                for c in node.children:
                    visit(c)

            for t in rep.tasks:
                visit(t)

        out.sort(key=lambda n: n.started_at or EPOCH)
        return out

    def all_child_session_ids(self) -> set[str]:
        """Every session_id that appears as a child task in any report."""
        out: set[str] = set()

        def collect(node: TaskNode) -> None:
            if node.session_id:
                out.add(node.session_id)
            for c in node.children:
                collect(c)

        for rep in self.all_reports():
            for t in rep.tasks:
                collect(t)
        return out

    def parent_chain(self, session_id: str) -> list[str]:
        """Return ancestors of ``session_id`` from immediate-parent first up to
        the root. Empty if ``session_id`` is itself a root.
        """
        # Build child -> parent map by walking every report's task tree.
        child_to_parent: dict[str, str] = {}

        def visit(node: TaskNode, parent_session: str) -> None:
            if node.session_id:
                child_to_parent.setdefault(node.session_id, parent_session)
            # Children of this node have node.session_id as their parent (when
            # node.session_id is known). Otherwise propagate the original parent.
            propagated = node.session_id or parent_session
            for c in node.children:
                visit(c, propagated)

        for rep in self.all_reports():
            for t in rep.tasks:
                visit(t, rep.parent_session)

        chain: list[str] = []
        visited: set[str] = set()
        cur = child_to_parent.get(session_id)
        while cur is not None and cur not in visited:
            visited.add(cur)
            chain.append(cur)
            cur = child_to_parent.get(cur)
        return chain

    def build_subtree(self, root_session_id: str) -> list[TaskNode]:
        """Return direct children of ``root_session_id``, with descendants.

        Uses ``_latest_view`` so each session appears once with its most
        recent snapshot. Re-running the same workflow won't duplicate
        cards/edges in the canvas tree.
        """
        canonical, children_sids, _ = self._latest_view()
        visited: set[str] = {root_session_id}

        def build(sid: str) -> Optional[TaskNode]:
            node = canonical.get(sid)
            if node is None:
                return None
            kids: list[TaskNode] = []
            for c_sid in children_sids.get(sid, []):
                if c_sid in visited:
                    continue
                visited.add(c_sid)
                child = build(c_sid)
                if child is not None:
                    kids.append(child)
            return TaskNode(
                session_id=node.session_id,
                task=node.task,
                status=node.status,
                depth=node.depth,
                duration_seconds=node.duration_seconds,
                summary=node.summary,
                error=node.error,
                children=kids,
                invoke_id=node.invoke_id,
                started_at=node.started_at,
                ended_at=node.ended_at,
                call_type=node.call_type,
            )

        out: list[TaskNode] = []
        for child_sid in children_sids.get(root_session_id, []):
            if child_sid in visited:
                continue
            visited.add(child_sid)
            built = build(child_sid)
            if built is not None:
                out.append(built)
        return out

    # --- parsing ----------------------------------------------------------

    def _load_report(self, path: Path) -> Optional[InvokeReport]:
        try:
            with path.open("r", encoding="utf-8") as fh:
                raw = yaml.safe_load(fh)
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(raw, dict):
            return None
        invoke_id = str(raw.get("invoke_id") or path.parent.name)
        parent = raw.get("parent_session")
        if not isinstance(parent, str) or not parent:
            return None
        started = _parse_ts(raw.get("started_at"))
        ended = _parse_ts(raw.get("ended_at"))
        status = str(raw.get("status") or "unknown")
        tasks = [
            _task_node(t, invoke_id, started, ended)
            for t in (raw.get("tasks") or [])
            if isinstance(t, dict)
        ]
        return InvokeReport(
            invoke_id=invoke_id,
            parent_session=parent,
            started_at=started,
            ended_at=ended,
            status=status,
            tasks=tasks,
            path=path,
        )


def _task_node(
    raw: dict[str, Any],
    invoke_id: str,
    parent_started: Optional[datetime],
    parent_ended: Optional[datetime],
) -> TaskNode:
    children_raw = raw.get("children") or []
    children = [
        _task_node(c, invoke_id, parent_started, parent_ended)
        for c in children_raw
        if isinstance(c, dict)
    ]
    raw_call_type = raw.get("call_type")
    call_type = raw_call_type if isinstance(raw_call_type, str) else "fork"
    return TaskNode(
        session_id=raw.get("session_id"),
        task=str(raw.get("task") or ""),
        status=str(raw.get("status") or "unknown"),
        depth=int(raw.get("depth") or 0),
        duration_seconds=_to_float(raw.get("duration_seconds")),
        summary=raw.get("summary") if isinstance(raw.get("summary"), str) else None,
        error=raw.get("error") if isinstance(raw.get("error"), str) else None,
        children=children,
        invoke_id=invoke_id,
        started_at=parent_started,
        ended_at=parent_ended,
        call_type=call_type,
    )


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


