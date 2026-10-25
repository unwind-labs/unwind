"""Contracts for adding a session source.

Implement SessionSource, then register one instance in sources.SOURCES. Keep
filesystem layout and wire-format details inside the adapter. Consumers use
SessionFile and the normalized transcript record schema documented below.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


@dataclass(frozen=True)
class SessionFile:
    sid: str
    path: Path
    mtime: float
    size: int
    provider: str
    cwd: str | None = None


class SessionSource(ABC):
    """Read-only adapter for one producer of session logs.

    Normalized records carry type, uuid, timestamp, sessionId, and provider.
    Conversation records use user/assistant/system and message.content blocks
    (text, thinking, tool_use, tool_result). A usage record carries message.model
    and disjoint message.usage counters. Usage records do not count as messages.
    Metadata records can carry cwd and gitBranch. IDs must be stable on append.
    """

    id: str
    label: str

    @abstractmethod
    def discover_projects(self) -> Iterable[tuple[str, Path]]:
        """Yield project slug and working directory (or a storage fallback)."""

    @abstractmethod
    def list_sessions(
        self, slug: str, project_dir: Path, *, fresh: bool = False
    ) -> Iterable[SessionFile]:
        """Yield sessions belonging to exactly this project."""

    @abstractmethod
    def owns_session(self, session_id: str) -> bool:
        """Whether this adapter owns this ID namespace."""

    def resolve(self, slug: str, project_dir: Path, session_id: str) -> Path | None:
        return next(
            (
                s.path
                for s in self.list_sessions(slug, project_dir)
                if s.sid == session_id
            ),
            None,
        )

    def matches_path(self, path: Path) -> bool:
        """Recognize non-JSONL storage before the fallback content probe.

        Override with read_records for formats such as a JSON export.
        """
        return False

    def read_records(self, path: Path) -> Iterator[dict]:
        """Read a transcript. Override when the source is not JSONL."""
        from ..jsonl import iter_lines

        yield from self.normalize(iter_lines(path))

    @abstractmethod
    def matches_record(self, first: dict) -> bool:
        """Recognize a file from its first valid JSON object."""

    @abstractmethod
    def normalize(self, records: Iterable[dict]) -> Iterator[dict]:
        """Translate source records into unwind's shared transcript schema."""

    def estimate_cost(
        self, model: str | None, cw: int, cr: int, r: int, w: int
    ) -> dict[str, float] | None:
        """Return category costs, or None when no reliable estimate exists."""
        return None

    def session_status(self) -> str | None:
        """Override Claude's process/callstack status inference if necessary.

        Sources without process tracking report done (historical/resumable).
        None delegates to the existing Claude process/callstack logic.
        """
        return "done"
