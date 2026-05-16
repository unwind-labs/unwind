"""``unwind project`` verbs: list, show, current, path."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import typer

from ..api.projects import ProjectSummary, fork_ids_for
from ..jsonl import EPOCH
from ..projects import ProjectPaths, claude_projects_root
from ..registry import (
    index_for_slug,
    last_activity_for,
    list_known_projects,
    register_default_project,
)
from ..server_state import default_source_path
from . import _common, _render

app = typer.Typer(no_args_is_help=True, add_completion=False)


def _build_summaries() -> list[ProjectSummary]:
    """Build the same list of ``ProjectSummary`` rows the HTTP endpoint returns."""
    out: list[ProjectSummary] = []
    for slug, source in list_known_projects():
        index = index_for_slug(slug)
        sessions = index.list_sessions()
        last_ts = sessions[0].last_timestamp if sessions else None
        if last_ts is None:
            mtime = last_activity_for(slug)
            if mtime is not None:
                last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
        real_cwd = next((s.cwd for s in sessions if s.cwd), None)
        source_path = real_cwd or str(source)
        # The session list pane hides callstack forks (they show up nested
        # in the call tree under their parent), so the picker count must do
        # the same — otherwise a project with 1 root + 10 forks advertises
        # "11 sessions" but only 1 actually appears when you open it.
        fork_ids = fork_ids_for(slug)
        visible = sum(1 for s in sessions if s.session_id not in fork_ids)
        out.append(
            ProjectSummary(
                slug=slug,
                source_path=source_path,
                last_activity=last_ts,
                session_count=visible,
            )
        )
    out.sort(
        key=lambda p: p.last_activity or EPOCH,
        reverse=True,
    )
    return out


@app.command("list")
def list_projects(
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
    limit: Optional[int] = typer.Option(None, "--limit", min=1),
) -> None:
    """List every known/discoverable project."""
    _common.validate_harness(harness)
    rows = _build_summaries()
    if limit is not None:
        rows = rows[:limit]
    if json_out:
        _common.echo_json([r.model_dump(mode="json") for r in rows])
    else:
        _render.render_project_table(rows)


@app.command("show")
def show_project(
    path: Optional[str] = typer.Argument(
        None, help="Project path (defaults to CWD)."
    ),
    slug: Optional[str] = typer.Option(None, "--slug", help="Resolve by slug instead."),
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show one project's slug, path, last activity, and session count."""
    _common.validate_harness(harness)
    if slug is not None and path is not None:
        raise _common.usage_error("pass either PATH or --slug, not both")
    arg = slug if slug is not None else path
    paths = _common.resolve_project(arg, harness)
    index = index_for_slug(paths.slug)
    sessions = index.list_sessions()
    last_ts = sessions[0].last_timestamp if sessions else None
    if last_ts is None:
        mtime = last_activity_for(paths.slug)
        if mtime is not None:
            last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
    real_cwd = next((s.cwd for s in sessions if s.cwd), None)
    fork_ids = fork_ids_for(paths.slug)
    visible = sum(1 for s in sessions if s.session_id not in fork_ids)
    summary = ProjectSummary(
        slug=paths.slug,
        source_path=real_cwd or str(paths.source_path),
        last_activity=last_ts,
        session_count=visible,
    )
    if json_out:
        _common.echo_json(summary.model_dump(mode="json"))
    else:
        _render.render_project_show(summary)


@app.command("current")
def current_project(
    harness: str = typer.Option("claude", "--harness"),
    json_out: bool = typer.Option(False, "--json"),
) -> None:
    """Show the current default project (env-provided or CWD)."""
    _common.validate_harness(harness)
    src = default_source_path()
    if src is None:
        src = str(Path.cwd().resolve())
    register_default_project(src)
    paths = ProjectPaths.for_path(src)
    index = index_for_slug(paths.slug)
    sessions = index.list_sessions()
    last_ts = sessions[0].last_timestamp if sessions else None
    if last_ts is None:
        mtime = last_activity_for(paths.slug)
        if mtime is not None:
            last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
    real_cwd = next((s.cwd for s in sessions if s.cwd), None)
    fork_ids = fork_ids_for(paths.slug)
    visible = sum(1 for s in sessions if s.session_id not in fork_ids)
    summary = ProjectSummary(
        slug=paths.slug,
        source_path=real_cwd or str(paths.source_path),
        last_activity=last_ts,
        session_count=visible,
    )
    if json_out:
        _common.echo_json(summary.model_dump(mode="json"))
    else:
        _render.render_project_show(summary)


@app.command("path")
def slug_to_path(
    slug: str = typer.Argument(...),
    harness: str = typer.Option("claude", "--harness"),
) -> None:
    """Reverse-lookup a slug to its on-disk project directory."""
    _common.validate_harness(harness)
    paths = ProjectPaths.for_slug(slug)
    if not paths.project_dir.is_dir():
        raise _common.not_found(f"slug {slug!r} not found under {claude_projects_root()}")
    typer.echo(str(paths.project_dir))
