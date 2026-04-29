"""Session index: list + cache of per-project sessions."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .jsonl import SessionSummary, extract_session_summary
from .projects import ProjectPaths


@dataclass
class _CacheEntry:
    summary: SessionSummary
    mtime: float
    size: int


class SessionIndex:
    """Keeps ``SessionSummary`` objects up-to-date for one project.

    Reads are cheap after the first pass: we reuse a cached summary whenever a
    JSONL's mtime and size haven't changed.
    """

    def __init__(self, paths: ProjectPaths) -> None:
        self._paths = paths
        self._cache: dict[str, _CacheEntry] = {}
        self._lock = threading.Lock()

    @property
    def paths(self) -> ProjectPaths:
        return self._paths

    def list_sessions(self) -> list[SessionSummary]:
        """Return every session for this project, sorted by start time (newest first)."""
        if not self._paths.project_dir.is_dir():
            return []
        results: list[SessionSummary] = []
        for jsonl in self._paths.project_dir.glob("*.jsonl"):
            summary = self._get_summary(jsonl)
            if summary is not None:
                results.append(summary)
        results.sort(
            key=lambda s: s.first_timestamp or datetime.fromtimestamp(0, timezone.utc),
            reverse=True,
        )
        return results

    def get_session(self, session_id: str) -> Optional[SessionSummary]:
        jsonl = self._paths.project_dir / f"{session_id}.jsonl"
        if not jsonl.is_file():
            return None
        return self._get_summary(jsonl)

    def jsonl_path_for(self, session_id: str) -> Optional[Path]:
        jsonl = self._paths.project_dir / f"{session_id}.jsonl"
        return jsonl if jsonl.is_file() else None

    def invalidate(self, session_id: str) -> None:
        with self._lock:
            self._cache.pop(session_id, None)

    # --- internals --------------------------------------------------------

    def _get_summary(self, jsonl: Path) -> Optional[SessionSummary]:
        try:
            stat = jsonl.stat()
        except OSError:
            return None
        session_id = jsonl.stem
        with self._lock:
            cached = self._cache.get(session_id)
            if (
                cached is not None
                and cached.mtime == stat.st_mtime
                and cached.size == stat.st_size
            ):
                return cached.summary
        summary = extract_session_summary(jsonl, session_id)
        if summary is None:
            return None
        with self._lock:
            self._cache[session_id] = _CacheEntry(
                summary=summary, mtime=stat.st_mtime, size=stat.st_size
            )
        return summary
