"""Project endpoints: list projects, resolve a slug."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..jsonl import EPOCH
from ..dialog import pick_folder
from ..security import require_trusted_origin
from ..projects import claude_projects_root, slug_for
from ..registry import (
    callstack_for_slug,
    fork_detector_for_slug,
    forget_slug,
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
    """Lightweight project listing for the picker.

    Avoids parsing any JSONLs: ``session_count`` is the raw on-disk file
    count and ``last_activity`` is the max ``*.jsonl`` mtime. Fork-filtering
    (which would require building the callstack + fork-detector indexes for
    every known project) is intentionally skipped here — the session list
    pane re-filters on open. For ~30 projects with ~100 sessions each this
    drops the endpoint from tens of thousands of JSONL parses to one scandir
    per project.
    """
    out: list[ProjectSummary] = []
    root = claude_projects_root()
    for slug, source in list_known_projects():
        project_dir = root / slug
        count, max_mtime, jsonl_paths = _scan_project_dir(project_dir)
        last_ts = (
            datetime.fromtimestamp(max_mtime, tz=timezone.utc)
            if max_mtime > 0
            else None
        )
        # Slug-only projects (entered via the picker, never through a real
        # path) carry a synthetic ``source_path`` equal to the slug dir.
        # Recover the real working directory from one session's ``cwd``
        # field so the UI can show a friendly folder name.
        is_synthetic = source == project_dir
        source_path = str(source)
        if is_synthetic:
            real_cwd = _peek_cwd_from_any(jsonl_paths)
            if real_cwd:
                source_path = real_cwd
        out.append(
            ProjectSummary(
                slug=slug,
                source_path=source_path,
                last_activity=last_ts,
                session_count=count,
            )
        )
    out.sort(
        key=lambda p: p.last_activity or EPOCH,
        reverse=True,
    )
    return out


def _scan_project_dir(project_dir: Path) -> tuple[int, float, list[Path]]:
    """One scandir pass: returns (jsonl_count, max_mtime, jsonl_paths).

    No JSONL parsing. ``max_mtime`` is 0.0 when the directory is empty or
    unreadable."""
    count = 0
    max_mtime = 0.0
    paths: list[Path] = []
    try:
        with os.scandir(project_dir) as it:
            for entry in it:
                if not entry.name.endswith(".jsonl"):
                    continue
                try:
                    if not entry.is_file():
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                count += 1
                if st.st_mtime > max_mtime:
                    max_mtime = st.st_mtime
                paths.append(Path(entry.path))
    except OSError:
        pass
    return count, max_mtime, paths


def _peek_cwd_from_any(jsonl_paths: list[Path]) -> Optional[str]:
    """Read up to a few lines of one JSONL each until a ``cwd`` field shows up.

    Cheap alternative to ``SessionIndex.list_sessions()`` which fully parses
    every JSONL. Sessions typically carry ``cwd`` on the first user/assistant
    record, so this usually reads <5 lines."""
    for path in jsonl_paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for i, raw in enumerate(fh):
                    if i > 20:
                        break
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    cwd = rec.get("cwd")
                    if isinstance(cwd, str) and cwd:
                        return cwd
        except OSError:
            continue
    return None


def fork_ids_for(slug: str) -> set[str]:
    """Return the set of session_ids that ``GET /projects/{slug}/sessions``
    would hide as callstack forks. Mirrors the logic in
    ``sessions_api.list_sessions`` so picker counts match list counts."""
    out: set[str] = set()
    ci = callstack_for_slug(slug)
    if ci.has_logs:
        out |= ci.all_child_session_ids()
    out |= fork_detector_for_slug(slug).fork_session_ids()
    return out


@router.get("/projects/default", response_model=DefaultProject)
def get_default_project() -> DefaultProject:
    path = default_source_path()
    if path is not None:
        register_default_project(path)
    return DefaultProject(slug=default_slug(), source_path=path)


@router.post(
    "/projects/pick-folder",
    response_model=PickedFolder,
    dependencies=[Depends(require_trusted_origin)],
)
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
