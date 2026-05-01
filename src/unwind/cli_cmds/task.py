"""``unwind task`` verbs: tree, list, roots, forks."""
from __future__ import annotations

from typing import Optional

import typer

from ..callstack import TaskNode
from ..registry import (
    callstack_for_slug,
    fork_detector_for_slug,
    index_for_slug,
    subagent_index_for_slug,
)
from . import _common, _render
from .session import build_session_tree

app = typer.Typer(no_args_is_help=True, add_completion=False)


_VALID_KINDS = {"call", "subagent", "all"}


def _validate_kind(kind: str) -> str:
    if kind not in _VALID_KINDS:
        raise _common.usage_error(
            "--kind must be one of: call, subagent, all"
        )
    return kind


def _filter_tree_kind(children: list[dict], kind: str) -> list[dict]:
    if kind == "all":
        return children
    out = []
    for c in children:
        c_kind = c.get("kind", "call")
        if c_kind == kind:
            new_c = dict(c)
            new_c["children"] = _filter_tree_kind(c.get("children") or [], kind)
            out.append(new_c)
        else:
            out.extend(_filter_tree_kind(c.get("children") or [], kind))
    return out


def _trim_depth(children: list[dict], depth_limit: Optional[int], depth: int = 0) -> list[dict]:
    if depth_limit is None:
        return children
    out = []
    for c in children:
        if depth >= depth_limit:
            break
        new_c = dict(c)
        new_c["children"] = _trim_depth(c.get("children") or [], depth_limit, depth + 1)
        out.append(new_c)
    return out


@app.command("tree")
def task_tree(
    session_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    kind: str = typer.Option("all", "--kind"),
    depth: Optional[int] = typer.Option(None, "--depth", min=1),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show the unified call+subagent tree rooted at this session."""
    _validate_kind(kind)
    paths = _common.resolve_project(project, harness)
    response = build_session_tree(paths.slug, session_id)
    payload = response.model_dump(mode="json")
    payload["children"] = _filter_tree_kind(payload["children"], kind)
    payload["children"] = _trim_depth(payload["children"], depth)
    if json_out:
        _common.echo_json(payload)
    else:
        _render.render_task_tree(session_id, payload["children"], depth_limit=depth)


@app.command("list")
def task_list(
    session_id: str = typer.Argument(...),
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    kind: str = typer.Option("all", "--kind"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List the direct children (calls + subagents) of a session."""
    _validate_kind(kind)
    paths = _common.resolve_project(project, harness)
    ci = callstack_for_slug(paths.slug)
    si = subagent_index_for_slug(paths.slug)

    nodes: list[TaskNode] = []
    if kind in ("call", "all"):
        nodes.extend(ci.direct_children_of(session_id))
    if kind in ("subagent", "all"):
        for inv in si.list_for_session(session_id):
            nodes.append(
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

    if json_out:
        _common.echo_json([n.to_dict() for n in nodes])
    else:
        if not nodes:
            typer.echo("(no tasks)")
            return
        for n in nodes:
            kind_tag = n.kind
            sid = n.session_id or "(unresolved)"
            typer.echo(f"{kind_tag:9s}  {n.status:10s}  {n.task}  {sid}")


@app.command("roots")
def task_roots(
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List top-level callstack root sessions in this project.

    A root is a ``parent_session`` of some report whose own session_id never
    appears as a child task in any other report.
    """
    paths = _common.resolve_project(project, harness)
    ci = callstack_for_slug(paths.slug)
    if not ci.has_logs:
        if json_out:
            _common.echo_json([])
            return
        typer.echo("(no callstack reports)")
        return

    children_ids = ci.all_child_session_ids()
    parent_sessions = {r.parent_session for r in ci.all_reports()}
    roots = sorted(parent_sessions - children_ids)
    if json_out:
        _common.echo_json(roots)
    else:
        if not roots:
            typer.echo("(no roots)")
            return
        for sid in roots:
            typer.echo(sid)


@app.command("forks")
def task_forks(
    project: Optional[str] = typer.Option(None, "--project"),
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """List in-flight forks not yet accounted for as callstack tasks."""
    paths = _common.resolve_project(project, harness)
    ci = callstack_for_slug(paths.slug)
    fd = fork_detector_for_slug(paths.slug)

    detected_forks = fd.fork_session_ids()
    accounted = ci.all_child_session_ids() if ci.has_logs else set()
    floating = sorted(detected_forks - accounted)
    # Verify the index has a known session for every floating id (skip stragglers).
    index = index_for_slug(paths.slug)
    floating = [s for s in floating if index.get_session(s) is not None]
    if json_out:
        _common.echo_json(floating)
    else:
        if not floating:
            typer.echo("(no unaccounted forks)")
            return
        for sid in floating:
            typer.echo(sid)
