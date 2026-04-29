"""CLI entry point: ``unwind`` command.

Single-command CLI. ``unwind [path] [--port 8765] [--no-browser] [--all]``.
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console

from .projects import ProjectPaths, claude_projects_root

console = Console()


def _pick_port(preferred: Optional[int]) -> int:
    if preferred is not None:
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _open_browser_later(url: str, delay_s: float = 0.4) -> None:
    def _go() -> None:
        time.sleep(delay_s)
        try:
            webbrowser.open(url)
        except Exception:
            pass

    threading.Thread(target=_go, daemon=True).start()


def _run(
    path: Optional[str] = typer.Argument(
        None,
        help="Project folder to observe. Defaults to the current working directory.",
        show_default=False,
    ),
    port: Optional[int] = typer.Option(
        None, "--port", "-p", help="Bind to a specific port (default: ephemeral)."
    ),
    host: str = typer.Option(
        "127.0.0.1", "--host", help="Bind address. Loopback only by default."
    ),
    no_browser: bool = typer.Option(
        False, "--no-browser", help="Print the URL instead of opening a browser."
    ),
    all_projects: bool = typer.Option(
        False, "--all", help="Show a project picker across every Claude project."
    ),
    log_level: str = typer.Option(
        "warning", "--log-level", help="Uvicorn log level."
    ),
    reload: bool = typer.Option(
        False, "--reload", help="Auto-reload on backend code changes (dev)."
    ),
) -> None:
    """Launch the unwind observer for the given folder (or CWD)."""
    chosen_path = Path(path).resolve() if path else Path.cwd().resolve()

    if all_projects:
        if not claude_projects_root().is_dir():
            console.print(
                "[red]~/.claude/projects not found — run Claude Code at least once first.[/]"
            )
            raise typer.Exit(1)
        query = ""
    else:
        paths = ProjectPaths.for_path(chosen_path)
        if not paths.has_project_dir:
            console.print(
                f"[yellow]No Claude sessions found for this folder yet.[/]\n"
                f"  path: {paths.source_path}\n"
                f"  slug: {paths.slug}\n"
                f"  looked in: {paths.project_dir}\n"
                "Will still launch — sessions will appear here once you run Claude Code in this folder."
            )
        query = f"?project={paths.slug}"

    chosen_port = _pick_port(port)
    url = f"http://{host}:{chosen_port}/{query}"

    os.environ["UNWIND_DEFAULT_PATH"] = str(chosen_path)
    if not all_projects:
        os.environ["UNWIND_DEFAULT_SLUG"] = ProjectPaths.for_path(chosen_path).slug

    console.print(f"[bold]unwind[/] serving on [cyan]{url}[/]")
    console.print(f"  project: {chosen_path}")
    console.print("  press Ctrl-C to stop\n")

    if not no_browser:
        _open_browser_later(url)

    try:
        uvicorn.run(
            "unwind.server:create_app",
            factory=True,
            host=host,
            port=chosen_port,
            log_level=log_level,
            reload=reload,
        )
    except KeyboardInterrupt:
        sys.exit(0)


def main() -> None:
    typer.run(_run)
