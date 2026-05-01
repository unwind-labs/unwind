"""Shared helpers for the noun-verb CLI: project resolution, harness validation,
exit-code mapping, JSON output."""
from __future__ import annotations

import enum
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

import typer

from ..projects import ProjectPaths, claude_projects_root
from ..registry import register_default_project


# Exit codes (per plan): 0 success, 1 not-found, 2 usage error.
EXIT_OK = 0
EXIT_NOT_FOUND = 1
EXIT_USAGE = 2


class Harness(str, enum.Enum):
    """Supported agent harnesses. Only ``claude`` is wired in v1."""

    claude = "claude"


def validate_harness(harness: str) -> None:
    """Raise a usage error if ``harness`` is not a supported value."""
    if harness != Harness.claude.value:
        typer.echo(
            f"error: --harness {harness!r} is not supported in v1 (only 'claude')",
            err=True,
        )
        raise typer.Exit(EXIT_USAGE)


def resolve_project(arg: Optional[str], harness: str) -> ProjectPaths:
    """Resolve a ``--project`` arg (or default CWD) to a ``ProjectPaths``.

    ``arg`` can be a path or a slug present under ``~/.claude/projects/``. A
    bare value with no path separator that matches a directory under the Claude
    projects root is treated as a slug; everything else is treated as a path.
    Registers the resolved source path with the registry so callstack-log
    discovery (auto-upgrade-from-synthetic-slug) runs identically to the
    server.
    """
    validate_harness(harness)
    raw = arg if arg is not None else os.getcwd()
    paths: ProjectPaths
    if "/" not in raw and "\\" not in raw:
        candidate = claude_projects_root() / raw
        if candidate.is_dir():
            paths = ProjectPaths.for_slug(raw)
        else:
            paths = ProjectPaths.for_path(Path(raw).expanduser().resolve())
    else:
        paths = ProjectPaths.for_path(Path(raw).expanduser().resolve())

    if not paths.has_project_dir:
        typer.echo(
            f"warning: no Claude sessions found for this project yet.\n"
            f"  path: {paths.source_path}\n"
            f"  slug: {paths.slug}\n"
            f"  looked in: {paths.project_dir}",
            err=True,
        )

    # Mirror the server-side behavior: register the real source path so the
    # registry's slug-to-source mapping skips the synthetic ``for_slug`` path.
    if paths.has_project_dir and paths.source_path.is_dir():
        try:
            register_default_project(str(paths.source_path))
        except Exception:
            # Registration is best-effort — it should never block a read command.
            pass
    return paths


def echo_json(payload: Any) -> None:
    """Print ``payload`` as a single JSON document on stdout."""
    typer.echo(json.dumps(payload, default=_json_default))


def _json_default(value: Any) -> Any:
    # Pydantic models go through ``model_dump`` first; this catches any leftover
    # types (datetime, Path, set) that the standard encoder rejects.
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def not_found(message: str) -> "typer.Exit":
    """Print ``message`` to stderr and return an exit-1 ``typer.Exit``."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(EXIT_NOT_FOUND)


def usage_error(message: str) -> "typer.Exit":
    """Print ``message`` to stderr and return an exit-2 ``typer.Exit``."""
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(EXIT_USAGE)


def stderr(message: str) -> None:
    print(message, file=sys.stderr)
