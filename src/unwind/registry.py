"""Process-wide registry of ``SessionIndex`` objects, keyed by slug.

The FastAPI app is a single process; we don't need a database. One index per
project slug we've been asked about, lazily created on first access.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from .callstack import CallstackIndex
from .fork_detect import ForkDetector
from .projects import ProjectPaths, claude_projects_root, slug_for
from .server_state import default_source_path
from .sessions import SessionIndex
from .subagents import SubagentIndex


_lock = threading.Lock()
_indices: dict[str, SessionIndex] = {}
_callstack: dict[str, CallstackIndex] = {}
_fork_detectors: dict[str, ForkDetector] = {}
_subagents: dict[str, SubagentIndex] = {}
_slug_to_source: dict[str, Path] = {}


def _auto_register_default() -> None:
    """Pick up the env-provided default path if nothing's been registered yet."""
    src = default_source_path()
    if src is None:
        return
    slug = slug_for(src)
    with _lock:
        _slug_to_source.setdefault(slug, Path(src))


def register_default_project(source_path: str) -> None:
    """Called by the CLI on startup so ``/api/projects`` knows about the CWD."""
    paths = ProjectPaths.for_path(source_path)
    with _lock:
        _slug_to_source.setdefault(paths.slug, paths.source_path)
        _indices.setdefault(paths.slug, SessionIndex(paths))


def forget_slug(slug: str) -> None:
    """Drop all cached per-slug state.

    Used when we've just learned the real source path for a slug whose index
    was previously created from the synthetic ``for_slug`` fallback (e.g. the
    project-list endpoint touches every directory under
    ``~/.claude/projects/`` and would otherwise pin a bad ``callstack_log_dir``
    into the cache).
    """
    with _lock:
        _indices.pop(slug, None)
        _callstack.pop(slug, None)
        _fork_detectors.pop(slug, None)
        _subagents.pop(slug, None)
        _slug_to_source.pop(slug, None)


def index_for_slug(slug: str) -> SessionIndex:
    _auto_register_default()
    with _lock:
        existing = _indices.get(slug)
        if existing is not None:
            return existing
        source = _slug_to_source.get(slug)
        if source is not None:
            paths = ProjectPaths.for_path(source)
        else:
            paths = ProjectPaths.for_slug(slug)
        index = SessionIndex(paths)
        _indices[slug] = index
        return index


def callstack_for_slug(slug: str) -> CallstackIndex:
    _auto_register_default()
    with _lock:
        existing = _callstack.get(slug)
        if existing is not None:
            return existing
    index = index_for_slug(slug)
    ci = CallstackIndex(index.paths.callstack_log_dir)
    with _lock:
        _callstack[slug] = ci
    return ci


def fork_detector_for_slug(slug: str) -> ForkDetector:
    _auto_register_default()
    with _lock:
        existing = _fork_detectors.get(slug)
        if existing is not None:
            return existing
    index = index_for_slug(slug)
    fd = ForkDetector(index.paths.project_dir)
    with _lock:
        _fork_detectors[slug] = fd
    return fd


def subagent_index_for_slug(slug: str) -> SubagentIndex:
    _auto_register_default()
    with _lock:
        existing = _subagents.get(slug)
        if existing is not None:
            return existing
    index = index_for_slug(slug)
    si = SubagentIndex(index.paths.project_dir)
    with _lock:
        _subagents[slug] = si
    return si


def list_known_projects() -> list[tuple[str, Path]]:
    """Return ``(slug, source_path)`` for every known or discoverable project.

    "Known" = projects we've already been pointed at. "Discoverable" = every
    directory under ``~/.claude/projects/``. The source path for discovered
    projects is synthesized from the slug (lossy — Claude's slug is not
    reversible unambiguously).
    """
    seen: dict[str, Path] = {}
    with _lock:
        seen.update(_slug_to_source)
    root = claude_projects_root()
    if root.is_dir():
        for child in root.iterdir():
            if child.is_dir() and child.name not in seen:
                seen[child.name] = child
    return sorted(seen.items(), key=lambda kv: kv[0])


def last_activity_for(slug: str) -> Optional[float]:
    """Max mtime across JSONLs in a project, for project-picker sorting."""
    index = index_for_slug(slug)
    project_dir = index.paths.project_dir
    if not project_dir.is_dir():
        return None
    mtimes = [p.stat().st_mtime for p in project_dir.glob("*.jsonl") if p.is_file()]
    return max(mtimes) if mtimes else None
