"""Resolve a filesystem path to Claude Code's on-disk project layout.

Claude stores per-project session JSONLs under ``~/.claude/projects/<slug>/``,
where ``<slug>`` is the absolute path with every character that isn't a
letter, digit, or hyphen replaced by ``-``. That covers ``/``, ``.``, ``_``,
spaces, parentheses, and other punctuation that can show up in real project
paths (e.g. ``/Users/me/work/04. mcp`` → ``-Users-me-work-04--mcp``). The
callstack plugin additionally writes invocation logs under
``<project>/.claude/callstack/log/<invoke_id>/``.
"""
from __future__ import annotations

import os
import re
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple


# Match Claude Code's slugging: anything that isn't [A-Za-z0-9-] becomes "-".
# This matches the observed behavior across paths with dots, underscores,
# spaces, and other punctuation. Hyphens already in the path are preserved.
_SLUG_RE = re.compile(r"[^A-Za-z0-9-]")


def slug_for(path: str | Path) -> str:
    """Mirror Claude Code's project slugging rules."""
    absolute = str(Path(path).resolve())
    return _SLUG_RE.sub("-", absolute)


def claude_projects_root() -> Path:
    return Path.home() / ".claude" / "projects"


def project_dir(path: str | Path) -> Path:
    return claude_projects_root() / slug_for(path)


def callstack_log_dir(path: str | Path) -> Path:
    return Path(path).resolve() / ".claude" / "callstack" / "log"


@dataclass(frozen=True)
class ProjectPaths:
    """Everything unwind needs to know about a project on disk."""

    source_path: Path
    slug: str
    project_dir: Path
    callstack_log_dir: Path

    @classmethod
    def for_path(cls, path: str | Path) -> "ProjectPaths":
        p = Path(path).resolve()
        return cls(
            source_path=p,
            slug=slug_for(p),
            project_dir=project_dir(p),
            callstack_log_dir=callstack_log_dir(p),
        )

    @classmethod
    def for_slug(cls, slug: str) -> "ProjectPaths":
        """Reverse-lookup from a slug alone.

        We don't have the original path, so ``source_path`` is synthesized and
        the callstack log dir is unavailable. Useful for ``--all`` project
        browsing where sessions are viewable but call trees may be partial.
        """
        root = claude_projects_root() / slug
        return cls(
            source_path=root,
            slug=slug,
            project_dir=root,
            callstack_log_dir=Path("/dev/null") / "no-callstack",
        )

    @property
    def has_project_dir(self) -> bool:
        return self.project_dir.is_dir()


class ProjectJsonl(NamedTuple):
    """One entry in a project's JSONL listing."""
    sid: str
    path: Path
    mtime: float
    size: int


_LISTING_TTL_SECONDS = 1.0
# Keyed by project_dir Path. Value: (last_scan_monotonic, dir_mtime, entries).
_listing_lock = threading.Lock()
_listings: dict[Path, tuple[float, float, tuple[ProjectJsonl, ...]]] = {}


def project_jsonl_listing(
    project_dir: Path, *, fresh: bool = False
) -> tuple[ProjectJsonl, ...]:
    """Cached ``(sid, path, mtime, size)`` listing of a project's JSONL files.

    Centralizes the ``*.jsonl`` directory walk that previously happened
    independently in ``SessionIndex.list_sessions``, ``_project_jsonl_signature``,
    ``last_activity_for``, ``compute_invoke_index_for_project``,
    ``ForkDetector._refresh``, and the watcher startup. Multiple consumers in
    one request now share a single ``os.scandir`` pass.

    Cache key is ``(project_dir, dir_mtime)`` with a 1 s TTL. Note that
    in-place writes to a child JSONL do NOT bump the directory mtime, so
    callers that need accurate fingerprints across in-place growth (e.g. an
    HTTP ETag) should pass ``fresh=True`` to bypass the cache.
    """
    if not project_dir.is_dir():
        return ()
    try:
        dir_mtime = project_dir.stat().st_mtime
    except OSError:
        return ()
    now = time.monotonic()
    if not fresh:
        with _listing_lock:
            cached = _listings.get(project_dir)
            if cached is not None:
                ts, cached_dir_mtime, entries = cached
                if now - ts < _LISTING_TTL_SECONDS and cached_dir_mtime == dir_mtime:
                    return entries

    new_entries: list[ProjectJsonl] = []
    try:
        with os.scandir(project_dir) as it:
            for de in it:
                if not de.name.endswith(".jsonl"):
                    continue
                try:
                    st = de.stat()
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode):
                    continue
                new_entries.append(
                    ProjectJsonl(
                        sid=de.name[: -len(".jsonl")],
                        path=Path(de.path),
                        mtime=st.st_mtime,
                        size=st.st_size,
                    )
                )
    except OSError:
        return ()
    new_entries.sort(key=lambda e: e.sid)
    result = tuple(new_entries)
    with _listing_lock:
        _listings[project_dir] = (now, dir_mtime, result)
    return result


def invalidate_jsonl_listing(project_dir: Path | None = None) -> None:
    """Drop the cached listing.

    With ``project_dir`` set, drops only that entry. With ``None``, drops the
    whole cache (used by test fixtures that reload modules).
    """
    with _listing_lock:
        if project_dir is None:
            _listings.clear()
        else:
            _listings.pop(project_dir, None)
