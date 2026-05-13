"""Process-wide registry of ``SessionIndex`` objects, keyed by slug.

The FastAPI app is a single process; we don't need a database. One index per
project slug we've been asked about, lazily created on first access.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Optional

from .callstack import CallstackIndex
from .canvas_tree import CanvasTreeBuilder
from .fork_detect import ForkDetector
from .projects import ProjectPaths, claude_projects_root, slug_for
from .server_state import default_source_path
from .sessions import SessionIndex
from .spawns import SpawnResolver
from .subagents import SubagentIndex


_lock = threading.Lock()
_indices: dict[str, SessionIndex] = {}
_callstack: dict[str, CallstackIndex] = {}
_fork_detectors: dict[str, ForkDetector] = {}
_subagents: dict[str, SubagentIndex] = {}
_canvas_builders: dict[str, CanvasTreeBuilder] = {}
_slug_to_source: dict[str, Path] = {}
# (signature, invoke_id → parent_session_id) per slug. The signature is the
# (mtime, size) summary of every JSONL in the project dir; rebuilds when
# anything moves.
_invoke_indexes: dict[str, tuple[tuple, dict[str, str]]] = {}


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
        _canvas_builders.pop(slug, None)
        _slug_to_source.pop(slug, None)
        _invoke_indexes.pop(slug, None)


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

    # If we built the index from a synthetic slug (no real path registered
    # yet), peek at any session's ``cwd`` and upgrade the registry. Without
    # this the callstack log dir stays pinned at ``/dev/null/no-callstack``
    # and ``report_for_invoke`` always returns None — so callstack invokes
    # never resolve their children and the UI shows "resolving…" forever.
    if source is None:
        real_cwd = _peek_session_cwd(index)
        if real_cwd is not None:
            _upgrade_to_real_path(slug, real_cwd)
            with _lock:
                return _indices[slug]
    return index


def _peek_session_cwd(index: SessionIndex) -> Optional[Path]:
    sessions = index.list_sessions()
    for s in sessions:
        if s.cwd:
            return Path(s.cwd)
    return None


def _upgrade_to_real_path(slug: str, real_path: Path) -> None:
    """Replace the synthetic-slug index with one rooted at the real cwd.

    Drops any cached per-slug state so subsequent ``callstack_for_slug``,
    ``fork_detector_for_slug`` etc. rebuild against the real
    ``.claude/callstack/log`` directory under the project.
    """
    with _lock:
        _indices.pop(slug, None)
        _callstack.pop(slug, None)
        _fork_detectors.pop(slug, None)
        _subagents.pop(slug, None)
        _slug_to_source[slug] = real_path
        paths = ProjectPaths.for_path(real_path)
        _indices[slug] = SessionIndex(paths)


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


def canvas_tree_builder_for_slug(slug: str) -> CanvasTreeBuilder:
    """Project-scoped canvas-tree builder. Caches per-session JSONL scans
    so subsequent canvas requests for the same project reuse them."""
    _auto_register_default()
    with _lock:
        existing = _canvas_builders.get(slug)
        if existing is not None:
            return existing
    index = index_for_slug(slug)
    builder = CanvasTreeBuilder(index.paths.project_dir)
    with _lock:
        _canvas_builders[slug] = builder
    return builder


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


def _project_jsonl_signature(project_dir: Path) -> tuple:
    """Stable fingerprint of every JSONL in ``project_dir``.

    Cheap to compute (one stat per file) but invalidates the moment any
    JSONL is created, deleted, grown, or modified.
    """
    if not project_dir.is_dir():
        return ()
    out: list[tuple[str, float, int]] = []
    for jsonl in project_dir.glob("*.jsonl"):
        try:
            st = jsonl.stat()
        except OSError:
            continue
        out.append((jsonl.name, st.st_mtime, st.st_size))
    out.sort()
    return tuple(out)


def invoke_index_for_slug(slug: str, project_dir: Path) -> dict[str, str]:
    """Return the slug-level ``invoke_id → parent_session_id`` map.

    Computed by scanning every project JSONL for callstack tool_use →
    tool_result envelopes. Cached at the registry; rebuilds when any
    JSONL's (mtime, size) changes.
    """
    from .spawns import compute_invoke_index_for_project

    sig = _project_jsonl_signature(project_dir)
    with _lock:
        cached = _invoke_indexes.get(slug)
        if cached is not None and cached[0] == sig:
            return cached[1]
    fresh = compute_invoke_index_for_project(project_dir)
    with _lock:
        _invoke_indexes[slug] = (sig, fresh)
    return fresh


def spawn_resolver_for_slug(slug: str) -> SpawnResolver:
    """Compose a fresh ``SpawnResolver`` over this slug's three indexes.

    The resolver caches its own per-instance result; the underlying
    indexes themselves cache by mtime. Cheap to instantiate per request,
    so we don't keep one in the registry — this guarantees each
    request sees the latest filesystem state.
    """
    index = index_for_slug(slug)
    project_dir = index.paths.project_dir
    return SpawnResolver(
        callstack_for_slug(slug),
        fork_detector_for_slug(slug),
        subagent_index_for_slug(slug),
        project_dir=project_dir,
        invoke_index=invoke_index_for_slug(slug, project_dir),
    )


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
