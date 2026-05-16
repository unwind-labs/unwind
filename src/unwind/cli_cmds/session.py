"""``unwind session`` verbs: list, show, tree."""
from __future__ import annotations

from typing import Optional

import typer

from ..api.projects import fork_ids_for
from ..jsonl import EPOCH
from ..api.sessions_api import SessionRow, TreeResponse
from ..callstack import TaskNode
from ..processes import session_status
from ..registry import (
    callstack_for_slug,
    fork_detector_for_slug,
    index_for_slug,
    subagent_index_for_slug,
)
from ..subagents import SUBAGENT_PREFIX
from . import _common, _render

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _row_for(summary, ci, project_path: Optional[str]) -> SessionRow:
    last_epoch = summary.last_timestamp.timestamp() if summary.last_timestamp else None
    if ci.has_logs:
        tlc = sum(
            len(rep.tasks) for rep in ci.reports_by_parent().get(summary.session_id, [])
        )
    else:
        tlc = 0
    # See sessions_api.list_sessions for the rationale behind this priority
    # — short version: terminal callstack status on a "root only" (main)
    # session must defer to live process detection so a session the user is
    # still driving doesn't get marked done after its last invoke completes.
    status = "done"
    cs_status = ci.aggregate_status_for_session(summary.session_id) if ci.has_logs else None
    if cs_status is not None:
        cs_norm = cs_status.lower()
        if cs_norm == "yielded":
            status = "yield"
        elif cs_norm in ("running", "in_progress"):
            status = "live"
        elif ci.is_callstack_task(summary.session_id):
            status = "done"
        else:
            status = session_status(summary.cwd or project_path, last_epoch)
    else:
        status = session_status(summary.cwd or project_path, last_epoch)
    return SessionRow(
        session_id=summary.session_id,
        title=summary.title,
        custom_title=summary.custom_title,
        first_timestamp=summary.first_timestamp,
        last_timestamp=summary.last_timestamp,
        message_count=summary.message_count,
        top_level_call_count=tlc,
        cwd=summary.cwd,
        git_branch=summary.git_branch,
        status=status,
    )


@app.command("list")
def list_sessions(
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    include_forks: bool = typer.Option(False, "--include-forks"),
    status: str = typer.Option(
        "all", "--status", help="Filter by status: live | yield | done | all."
    ),
    limit: Optional[int] = typer.Option(None, "--limit", min=1),
    sort: str = typer.Option(
        "recent", "--sort", help="Order by 'recent' (last activity) or 'created'."
    ),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List sessions in a project."""
    if status not in ("live", "yield", "done", "all"):
        raise _common.usage_error("--status must be one of: live, yield, done, all")
    if sort not in ("recent", "created"):
        raise _common.usage_error("--sort must be one of: recent, created")

    paths = _common.resolve_project(project, harness)
    index = index_for_slug(paths.slug)
    project_path = str(index.paths.source_path) if index.paths.has_project_dir else None

    ci = callstack_for_slug(paths.slug)
    fork_ids: set[str] = set() if include_forks else fork_ids_for(paths.slug)

    rows: list[SessionRow] = []
    for s in index.list_sessions():
        if s.session_id in fork_ids:
            continue
        rows.append(_row_for(s, ci, project_path))

    if status != "all":
        rows = [r for r in rows if r.status == status]

    if sort == "created":
        rows.sort(key=lambda r: r.first_timestamp or EPOCH, reverse=True)
    else:
        rows.sort(key=lambda r: r.last_timestamp or EPOCH, reverse=True)

    if limit is not None:
        rows = rows[:limit]

    if json_out:
        _common.echo_json([r.model_dump(mode="json") for r in rows])
    else:
        _render.render_session_table(rows)


@app.command("show")
def show_session(
    session_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show one session's metadata."""
    paths = _common.resolve_project(project, harness)
    index = index_for_slug(paths.slug)
    summary = index.get_session(session_id)
    if summary is None:
        raise _common.not_found(f"session {session_id!r} not found in {paths.slug}")
    ci = callstack_for_slug(paths.slug)
    project_path = str(index.paths.source_path) if index.paths.has_project_dir else None
    row = _row_for(summary, ci, project_path)
    if json_out:
        _common.echo_json(row.model_dump(mode="json"))
    else:
        _render.render_session_show(row)


def _resolve_in_flight(
    children: list[TaskNode], slug: str, root_session_id: str
) -> None:
    fd = fork_detector_for_slug(slug)
    si = subagent_index_for_slug(slug)

    def visit(node: TaskNode, parent_sid: str) -> None:
        if not node.session_id and parent_sid and node.task:
            sid = fd.find_session_by_divergence_text(parent_sid, node.task)
            if sid is not None:
                node.session_id = sid
        for c in node.children:
            visit(c, node.session_id or parent_sid)
        if node.session_id and not node.session_id.startswith(SUBAGENT_PREFIX):
            for inv in si.list_for_session(node.session_id):
                node.children.append(
                    TaskNode(
                        session_id=inv.synthetic_session_id,
                        task=inv.description,
                        status="complete",
                        depth=node.depth + 1,
                        duration_seconds=None,
                        summary=f"{inv.agent_type} · {inv.message_count} msgs",
                        error=None,
                        started_at=inv.created_at,
                        kind="subagent",
                    )
                )

    for ch in children:
        visit(ch, root_session_id)


def build_session_tree(slug: str, session_id: str) -> TreeResponse:
    """Mirror the HTTP /tree endpoint logic, returning a ``TreeResponse``."""
    ci = callstack_for_slug(slug)
    children = ci.build_subtree(session_id)
    fd = fork_detector_for_slug(slug)
    si = subagent_index_for_slug(slug)

    _resolve_in_flight(children, slug, session_id)

    if not children:
        fork_sids = fd.children_of(session_id)
        if fork_sids:
            fd.find_session_by_divergence_text(session_id, "")
            for fork_sid in fork_sids:
                text = fd.divergence_text_for(fork_sid)
                children.append(
                    TaskNode(
                        session_id=fork_sid,
                        task=(text or "(in-flight)").strip(),
                        status="running",
                        depth=1,
                        duration_seconds=None,
                        summary=None,
                        error=None,
                        invoke_id=None,
                    )
                )

    if not session_id.startswith(SUBAGENT_PREFIX):
        for inv in si.list_for_session(session_id):
            children.append(
                TaskNode(
                    session_id=inv.synthetic_session_id,
                    task=inv.description,
                    status="complete",
                    depth=1,
                    duration_seconds=None,
                    summary=f"{inv.agent_type} · {inv.message_count} msgs",
                    error=None,
                    started_at=inv.created_at,
                    kind="subagent",
                )
            )

    return TreeResponse(
        session_id=session_id,
        children=[n.to_dict() for n in children],
        has_callstack_logs=ci.has_logs,
    )


def _trim_depth(children: list[dict], depth_limit: Optional[int], depth: int = 0) -> list[dict]:
    if depth_limit is None:
        return children
    out = []
    for c in children:
        if depth >= depth_limit:
            break
        new_c = dict(c)
        new_c["children"] = _trim_depth(
            c.get("children") or [], depth_limit, depth + 1
        )
        out.append(new_c)
    return out


@app.command("tree")
def session_tree(
    session_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    depth: Optional[int] = typer.Option(None, "--depth", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Render the call/subagent tree rooted at this session."""
    paths = _common.resolve_project(project, harness)
    index = index_for_slug(paths.slug)
    if (
        not session_id.startswith(SUBAGENT_PREFIX)
        and index.get_session(session_id) is None
    ):
        raise _common.not_found(f"session {session_id!r} not found in {paths.slug}")
    response = build_session_tree(paths.slug, session_id)
    response_dict = response.model_dump(mode="json")
    response_dict["children"] = _trim_depth(response_dict["children"], depth)
    if json_out:
        _common.echo_json(response_dict)
    else:
        _render.render_task_tree(
            session_id, response_dict["children"], depth_limit=depth
        )
