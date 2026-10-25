"""Session-source registry. Register new adapters here; consumers stay generic."""

from pathlib import Path

from .base import SessionFile, SessionSource
from .claude import ClaudeSource
from .codex import CodexSource

# Specific namespaces/formats precede the legacy Claude fallback.
SOURCES: tuple[SessionSource, ...] = (CodexSource(), ClaudeSource())


def source_for_session(session_id: str) -> SessionSource:
    return next(s for s in SOURCES if s.owns_session(session_id))


def source_for_record(record: dict) -> SessionSource:
    return next(s for s in SOURCES if s.matches_record(record))


def source_for_path(path: Path) -> SessionSource:
    for source in SOURCES:
        if source.matches_path(path):
            return source
    from ..jsonl import iter_lines

    records = iter_lines(path)
    try:
        first = next(records, {})
    finally:
        records.close()
    return source_for_record(first)


def source_by_id(provider: str) -> SessionSource:
    return next(s for s in SOURCES if s.id == provider)


def list_session_files(
    slug: str, project_dir: Path, *, fresh: bool = False
) -> tuple[SessionFile, ...]:
    return tuple(
        entry
        for source in SOURCES
        for entry in source.list_sessions(slug, project_dir, fresh=fresh)
    )


def resolve_session_path(slug: str, project_dir: Path, session_id: str) -> Path | None:
    return source_for_session(session_id).resolve(slug, project_dir, session_id)


def discover_projects() -> dict[str, Path]:
    # Prefer actual working directories over Claude's synthetic storage path.
    result: dict[str, Path] = {}
    for source in reversed(SOURCES):
        result.update(source.discover_projects())
    return result
