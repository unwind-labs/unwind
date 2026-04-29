"""Project endpoints: list projects, resolve a slug."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter
from pydantic import BaseModel

from ..dialog import pick_folder
from ..projects import slug_for
from ..registry import (
    forget_slug,
    index_for_slug,
    last_activity_for,
    list_known_projects,
    register_default_project,
)
from ..server_state import default_source_path, default_slug
from ..watcher import stop_watcher

router = APIRouter(tags=["projects"])


class ProjectSummary(BaseModel):
    slug: str
    source_path: str
    last_activity: Optional[datetime]
    session_count: int


class DefaultProject(BaseModel):
    slug: Optional[str]
    source_path: Optional[str]


class PickedFolder(BaseModel):
    """Result of the system folder-picker dialog.

    ``cancelled`` is ``True`` when the user dismissed the dialog; in that case
    ``slug`` and ``source_path`` are ``None``.
    """

    cancelled: bool
    slug: Optional[str] = None
    source_path: Optional[str] = None


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects() -> list[ProjectSummary]:
    out: list[ProjectSummary] = []
    for slug, source in list_known_projects():
        index = index_for_slug(slug)
        sessions = index.list_sessions()
        last_ts = sessions[0].last_timestamp if sessions else None
        if last_ts is None:
            mtime = last_activity_for(slug)
            if mtime is not None:
                last_ts = datetime.fromtimestamp(mtime, tz=timezone.utc)
        out.append(
            ProjectSummary(
                slug=slug,
                source_path=str(source),
                last_activity=last_ts,
                session_count=len(sessions),
            )
        )
    out.sort(
        key=lambda p: p.last_activity or datetime.fromtimestamp(0, tz=timezone.utc),
        reverse=True,
    )
    return out


@router.get("/projects/default", response_model=DefaultProject)
def get_default_project() -> DefaultProject:
    path = default_source_path()
    if path is not None:
        register_default_project(path)
    return DefaultProject(slug=default_slug(), source_path=path)


@router.post("/projects/pick-folder", response_model=PickedFolder)
def pick_folder_endpoint() -> PickedFolder:
    """Open a native folder-picker on the host and register the result.

    The dialog blocks the request until the user picks or cancels. On pick we
    register the folder so future ``/api/projects`` calls surface its real
    source path (not just a slug-derived synthetic one).
    """
    chosen = pick_folder()
    if chosen is None:
        return PickedFolder(cancelled=True)
    # The slug may already have a cached index built from the synthetic
    # ``for_slug`` fallback (the projects-list endpoint visits every directory
    # under ``~/.claude/projects/``). Drop everything for this slug so the
    # next request rebuilds against the real source path.
    slug = slug_for(chosen)
    stop_watcher(slug)
    forget_slug(slug)
    register_default_project(str(chosen))
    return PickedFolder(
        cancelled=False,
        slug=slug,
        source_path=str(chosen),
    )
