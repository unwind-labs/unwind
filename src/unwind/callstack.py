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
from datetime import datetime, timezone  # noqa: F401
from pathlib import Path
from typing import Any, Optional

import yaml


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
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, InvokeReport]] = {}

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
            rep = self._load(report_path)
            if rep is not None:
                out.append(rep)
        return out

    def reports_by_parent(self) -> dict[str, list[InvokeReport]]:
        by_parent: dict[str, list[InvokeReport]] = {}
        for rep in self.all_reports():
            by_parent.setdefault(rep.parent_session, []).append(rep)
        return by_parent

    def children_for_invoke(self, invoke_id: str) -> list[str]:
        """Return top-level task session_ids for a given ``invoke_id``."""
        rep = self.report_for_invoke(invoke_id)
        if rep is None:
            return []
        return [t.session_id for t in rep.tasks if t.session_id]

    def report_for_invoke(self, invoke_id: str) -> Optional[InvokeReport]:
        for rep in self.all_reports():
            if rep.invoke_id == invoke_id:
                return rep
        return None

    def task_status_for_session(self, session_id: str) -> Optional[str]:
        """Return the callstack task status for a session, if any report has it.

        Walks every report's task tree (and matches root-callers by
        ``parent_session``). Returns the task's ``status`` (e.g.
        ``complete``, ``running``, ``failed``); returns ``None`` if no
        report references this session.
        """

        def visit(node: TaskNode) -> Optional[str]:
            if node.session_id == session_id:
                return node.status or None
            for c in node.children:
                hit = visit(c)
                if hit is not None:
                    return hit
            return None

        for rep in self.all_reports():
            if rep.parent_session == session_id:
                # Root caller — its overall status is the report's status.
                return rep.status or None
            for t in rep.tasks:
                hit = visit(t)
                if hit is not None:
                    return hit
        return None

    def reports_with_session_node(self, session_id: str) -> list[InvokeReport]:
        """Reports whose task tree contains a node with ``session_id``.

        Used for nested invokes: a session deeper in the tree (e.g. /task-c)
        appears as a TaskNode inside a report whose parent_session is the
        outer root. Sorted by ``started_at`` ascending.
        """
        out: list[InvokeReport] = []

        def has_node(node: TaskNode) -> bool:
            if node.session_id == session_id:
                return True
            return any(has_node(c) for c in node.children)

        for rep in self.all_reports():
            if rep.parent_session == session_id:
                out.append(rep)
                continue
            if any(has_node(t) for t in rep.tasks):
                out.append(rep)

        out.sort(key=lambda r: r.started_at or datetime.fromtimestamp(0, timezone.utc))
        return out

    def children_in_report(
        self, report: InvokeReport, session_id: str
    ) -> list["TaskNode"]:
        """Return ``session_id``'s direct children inside ``report``.

        For a top-level (root) caller whose parent_session matches, returns
        ``report.tasks``. For nested callers, walks the task tree to find the
        matching node and returns its children.
        """
        if report.parent_session == session_id:
            return list(report.tasks)
        out: list[TaskNode] = []

        def visit(node: TaskNode) -> None:
            if node.session_id == session_id:
                out.extend(node.children)
                return
            for c in node.children:
                visit(c)

        for t in report.tasks:
            visit(t)
        return out

    def direct_children_of(self, session_id: str) -> list["TaskNode"]:
        """Find this session's direct children across every report.

        Two cases:

        1. ``session_id`` is the root caller of an invocation (i.e. matches
           ``rep.parent_session``). Return ``rep.tasks`` — the top-level
           tasks of that invocation. This is the LIVE-friendly path: the
           report.yaml is updated incrementally as children spawn, and we
           don't need to wait for the parent's tool_result to come back.

        2. ``session_id`` appears as a node deeper in some report's task
           tree (e.g. /task-c inside root's report). Return that node's
           children.
        """
        out: list[TaskNode] = []

        def visit(node: TaskNode) -> None:
            if node.session_id == session_id:
                out.extend(node.children)
                return  # don't recurse into already-found subtree
            for c in node.children:
                visit(c)

        for rep in self.all_reports():
            if rep.parent_session == session_id:
                out.extend(rep.tasks)
                continue
            for t in rep.tasks:
                visit(t)
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
        """Return direct children of ``root_session_id``, with descendants."""
        by_parent = self.reports_by_parent()
        return self._descend(root_session_id, by_parent, visited=set())

    def _descend(
        self,
        session_id: str,
        by_parent: dict[str, list[InvokeReport]],
        visited: set[str],
    ) -> list[TaskNode]:
        if session_id in visited:
            return []
        visited.add(session_id)
        out: list[TaskNode] = []
        for report in by_parent.get(session_id, []):
            for task in report.tasks:
                # The YAML already nests descendants from this invocation. We
                # also descend into sessions' own later invocations.
                merged = self._merge_task_with_later_invocations(
                    task, by_parent, visited
                )
                out.append(merged)
        return out

    def _merge_task_with_later_invocations(
        self,
        task: TaskNode,
        by_parent: dict[str, list[InvokeReport]],
        visited: set[str],
    ) -> TaskNode:
        own_children = [
            self._merge_task_with_later_invocations(c, by_parent, visited)
            for c in task.children
        ]
        if task.session_id:
            # If this session kicked off its own independent invocations later,
            # merge those in as additional children.
            later = self._descend(task.session_id, by_parent, visited)
            own_children.extend(later)
        return TaskNode(
            session_id=task.session_id,
            task=task.task,
            status=task.status,
            depth=task.depth,
            duration_seconds=task.duration_seconds,
            summary=task.summary,
            error=task.error,
            children=own_children,
            invoke_id=task.invoke_id,
            started_at=task.started_at,
            ended_at=task.ended_at,
        )

    # --- parsing ----------------------------------------------------------

    def _load(self, path: Path) -> Optional[InvokeReport]:
        try:
            stat = path.stat()
        except OSError:
            return None
        with self._lock:
            cached = self._cache.get(str(path))
            if cached is not None and cached[0] == stat.st_mtime:
                return cached[1]
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
        rep = InvokeReport(
            invoke_id=invoke_id,
            parent_session=parent,
            started_at=started,
            ended_at=ended,
            status=status,
            tasks=tasks,
            path=path,
        )
        with self._lock:
            self._cache[str(path)] = (stat.st_mtime, rep)
        return rep


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
    )


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_ts(v: Any) -> Optional[datetime]:
    if not isinstance(v, str):
        return None
    try:
        if v.endswith("Z"):
            v = v[:-1] + "+00:00"
        return datetime.fromisoformat(v).astimezone(timezone.utc)
    except ValueError:
        return None
