"""Resolve a filesystem path to Claude Code's on-disk project layout.

Claude stores per-project session JSONLs under ``~/.claude/projects/<slug>/``,
where ``<slug>`` is the absolute path with ``/``, ``.``, ``_`` all replaced by
``-``. The callstack plugin additionally writes invocation logs under
``<project>/.claude/callstack/log/<invoke_id>/``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


_SLUG_RE = re.compile(r"[/._]")


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
