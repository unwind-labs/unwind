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
from .projects import (
    ProjectPaths,
    claude_projects_root,
    project_jsonl_listing,
    slug_for,
)
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
# (signature, invoke_id → [candidate_session_id, ...]) per slug. The
# signature is the (mtime, size) summary of every JSONL in the project
# dir; rebuilds when anything moves.
_invoke_indexes: dict[str, tuple[tuple, dict[str, list[str]]]] = {}

# All per-slug auxiliary caches (everything except _indices, which has its own
# synthetic-slug upgrade logic). Listed once so forget/upgrade stay in sync
# with the per-slug accessors.
_PER_SLUG_CACHES: tuple[dict, ...] = (
    _callstack,
    _fork_detectors,
    _subagents,
    _canvas_builders,
)


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
        for cache in _PER_SLUG_CACHES:
            cache.pop(slug, None)
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
    # and report lookups all miss — so callstack invokes never resolve their
    # children and the UI shows "resolving…" forever.
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
        for cache in _PER_SLUG_CACHES:
            cache.pop(slug, None)
        _slug_to_source[slug] = real_path
        paths = ProjectPaths.for_path(real_path)
        _indices[slug] = SessionIndex(paths)


def _per_slug(cache: dict, slug: str, factory):
    """Lazy double-checked-locked get-or-create for a per-slug cache.

    Holds ``_lock`` only across map access; ``factory(SessionIndex)`` runs
    unlocked so it can call back into the registry (notably ``index_for_slug``
    which may upgrade synthetic slugs). Concurrent callers race-build and
    ``setdefault`` picks one winner.
    """
    _auto_register_default()
    with _lock:
        existing = cache.get(slug)
        if existing is not None:
            return existing
    index = index_for_slug(slug)
    obj = factory(index)
    with _lock:
        return cache.setdefault(slug, obj)


def callstack_for_slug(slug: str) -> CallstackIndex:
    return _per_slug(_callstack, slug, lambda idx: CallstackIndex(idx.paths.callstack_log_dir))


def fork_detector_for_slug(slug: str) -> ForkDetector:
    def make(idx) -> ForkDetector:
        # Inject the canvas builder's mtime-cached scanner so
        # divergence_text_for reads from the canonical SessionScan
        # instead of re-walking each fork JSONL.
        builder = canvas_tree_builder_for_slug(slug)
        return ForkDetector(
            idx.paths.project_dir,
            session_scanner=builder.get_scan,
        )

    return _per_slug(_fork_detectors, slug, make)


def canvas_tree_builder_for_slug(slug: str) -> CanvasTreeBuilder:
    """Project-scoped canvas-tree builder. Caches per-session JSONL scans
    so subsequent canvas requests for the same project reuse them."""
    return _per_slug(_canvas_builders, slug, lambda idx: CanvasTreeBuilder(idx.paths.project_dir))


def subagent_index_for_slug(slug: str) -> SubagentIndex:
    return _per_slug(_subagents, slug, lambda idx: SubagentIndex(idx.paths.project_dir))


def _project_jsonl_signature(project_dir: Path) -> tuple:
    """Stable fingerprint of every JSONL in ``project_dir``.

    Cheap to compute (one stat per file) but invalidates the moment any
    JSONL is created, deleted, grown, or modified.
    """
    # Bypass the listing cache: in-place writes to a child JSONL don't bump
    # the directory mtime, so the cached listing's stats could be stale.
    # Callers use this fingerprint as an HTTP ETag and cache key — staleness
    # masks real changes.
    return tuple(
        (e.path.name, e.mtime, e.size)
        for e in project_jsonl_listing(project_dir, fresh=True)
    )


def _callstack_log_signature(callstack_log_dir: Path) -> tuple:
    """Stable fingerprint of every report.yaml under ``callstack_log_dir``.

    Captures both new invocations (new directories) and updates to
    existing reports (status / task tree mutations after spawn).
    """
    if not callstack_log_dir.is_dir():
        return ()
    out: list[tuple[str, float, int]] = []
    try:
        # Each invocation lives in its own subdir; we only care about the
        # report files.
        for report in callstack_log_dir.glob("*/report.yaml"):
            try:
                st = report.stat()
            except OSError:
                continue
            out.append((report.parent.name, st.st_mtime, st.st_size))
    except OSError:
        return ()
    out.sort()
    return tuple(out)


def project_state_signature(slug: str) -> tuple:
    """Cheap project-state fingerprint suitable for HTTP ETags.

    Stat-based; never reads or parses any file. Invalidates on:

    * Any JSONL created/grown/deleted (covers new messages + new sessions)
    * Any callstack report created/updated (covers new spawns + status
      transitions)
    """
    index = index_for_slug(slug)
    return (
        _project_jsonl_signature(index.paths.project_dir),
        _callstack_log_signature(index.paths.callstack_log_dir),
    )


def invoke_index_for_slug(slug: str, project_dir: Path) -> dict[str, list[str]]:
    """Return the slug-level ``invoke_id → [candidate_session_id, ...]`` map.

    Computed by scanning every project JSONL for callstack tool_use →
    tool_result envelopes. Multiple sessions can carry the same
    invoke_id (callstack plugin echoes the OUTER invoke_id in inner
    ``/call`` tool_results from forked children); consumers pick the
    right candidate with extra context. Cached at the registry;
    rebuilds when any JSONL's (mtime, size) changes.
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
    builder = canvas_tree_builder_for_slug(slug)
    return SpawnResolver(
        callstack_for_slug(slug),
        fork_detector_for_slug(slug),
        subagent_index_for_slug(slug),
        project_dir=project_dir,
        invoke_index=invoke_index_for_slug(slug, project_dir),
        # Share the canvas-tree builder's mtime-cached scans so fork
        # status inference doesn't re-walk each child JSONL.
        session_scanner=builder.get_scan,
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
    entries = project_jsonl_listing(index.paths.project_dir)
    return max((e.mtime for e in entries), default=None)
