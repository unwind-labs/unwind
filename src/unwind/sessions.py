"""Session index: list + cache of per-project sessions."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Optional

from .jsonl import (
    EPOCH,
    SessionSummary,
    extract_session_summary,
    parse_ts,
    turn_delta,
)
from .projects import ProjectPaths, project_jsonl_listing


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
        """Return every session for this project, sorted by last activity
        (newest first). ``last_timestamp`` reflects ongoing work — a
        session that's been actively used today should outrank one
        that started earlier today but has been idle for hours."""
        if not self._paths.project_dir.is_dir():
            return []
        results: list[SessionSummary] = []
        for entry in project_jsonl_listing(self._paths.project_dir):
            summary = self._get_summary(entry.path)
            if summary is not None:
                results.append(summary)
        results.sort(
            key=lambda s: s.last_timestamp or s.first_timestamp or EPOCH,
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

    def apply_increment(
        self,
        session_id: str,
        new_records: list[dict[str, Any]],
        new_size: int,
        new_mtime: float,
    ) -> Optional[SessionSummary]:
        """Fold ``new_records`` into the cached summary without re-parsing.

        Returns the updated summary, or ``None`` if no cached entry exists
        (caller should fall back to a full parse via ``get_session``).
        Only fields cheaply derivable from the appended records are updated:
        ``message_count``, ``last_timestamp``, ``custom_title``/``title``,
        and ``file_size_bytes``. Bootstrap-only fields (``cwd``,
        ``git_branch``, ``first_timestamp``, original first-user title) are
        kept as-is from the cached summary.
        """
        with self._lock:
            cached = self._cache.get(session_id)
        if cached is None:
            return None
        summary = cached.summary

        added = 0
        last_ts = summary.last_timestamp
        custom_title = summary.custom_title
        # Thread the prior turn's assistant id so a turn whose block-split
        # records straddle the append boundary isn't counted twice.
        last_assistant_id = summary.last_assistant_id
        for rec in new_records:
            rtype = rec.get("type")
            if rtype == "custom-title":
                ct = rec.get("customTitle")
                if isinstance(ct, str) and ct.strip():
                    custom_title = ct.strip()
                continue
            ts = parse_ts(rec.get("timestamp"))
            if ts is not None and (last_ts is None or ts > last_ts):
                last_ts = ts
            delta, last_assistant_id = turn_delta(rec, last_assistant_id)
            added += delta

        title = custom_title or summary.title
        updated = replace(
            summary,
            message_count=summary.message_count + added,
            last_timestamp=last_ts,
            custom_title=custom_title,
            title=title,
            file_size_bytes=new_size,
            last_assistant_id=last_assistant_id,
        )
        with self._lock:
            self._cache[session_id] = _CacheEntry(
                summary=updated, mtime=new_mtime, size=new_size
            )
        return updated

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
