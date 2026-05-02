"""Detect whether any ``claude`` process is running in a given project cwd.

We can't map a running Claude process to its session_id (Claude doesn't expose
the id externally), so status is scoped to the project as a whole. A session's
own ``live/idle/done`` is then derived from JSONL mtime + "any claude running
in this project".
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psutil


# Tight window: only meant to bridge the brief gap between a claude session
# starting and its process becoming visible to ``project_activity`` via cwd.
# Anything wider misleads after the user exits — process detection has gone
# away but JSONL mtime is still recent. Aligned with the frontend's 30s
# polling cadence so the UI flips to done within one refresh of exit.
LIVE_MTIME_WINDOW_SEC = 30.0


@dataclass(frozen=True)
class ProjectActivity:
    claude_running: bool
    pid_count: int
    sampled_at: float


# Simple TTL cache so ``list_sessions`` can be called cheaply.
_CACHE_TTL_SEC = 2.0
_cache: dict[str, tuple[float, ProjectActivity]] = {}


def project_activity(project_path: str) -> ProjectActivity:
    now = time.time()
    key = str(Path(project_path).resolve())
    cached = _cache.get(key)
    if cached is not None and now - cached[0] < _CACHE_TTL_SEC:
        return cached[1]
    pids = 0
    for proc in psutil.process_iter(attrs=["name", "cmdline"]):
        try:
            info = proc.info
            if not _is_claude_process(info.get("name"), info.get("cmdline")):
                continue
            cwd = _safe_cwd(proc)
            if cwd == key:
                pids += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    result = ProjectActivity(
        claude_running=pids > 0, pid_count=pids, sampled_at=now
    )
    _cache[key] = (now, result)
    return result


def _is_claude_process(
    name: Optional[str], cmdline: Optional[list]
) -> bool:
    """Whether a process appears to be a Claude Code CLI invocation.

    macOS quirk: ``psutil.name()`` for the bare ``claude`` CLI sometimes
    returns the version string (e.g. ``"2.1.126"``) rather than ``claude``,
    so we can't rely on ``name`` alone. Match on the cmdline's argv[0]
    basename too — that catches both ``claude`` (PATH-resolved) and
    ``/Users/.../claude.app/Contents/MacOS/claude`` (full path).
    """
    n = (name or "").lower()
    if n == "claude":
        return True
    if not cmdline:
        return False
    head = cmdline[0] if isinstance(cmdline[0], str) else None
    if not head:
        return False
    # Direct command name (PATH-resolved).
    if head == "claude":
        return True
    # Absolute or relative path ending in ``/claude``.
    if head.endswith("/claude"):
        return True
    return False


def session_status(
    project_path: Optional[str], last_activity_epoch: Optional[float]
) -> str:
    """Return ``live | idle | done`` for a session.

    Priority:
    1. ``live`` — a ``claude`` process is currently running with this
       project as cwd (caught even if the session JSONL is quiet).
    2. ``live`` — fallback: JSONL modified within the last 5 minutes
       (catches sessions just after the user's prompt is sent but before
       the claude process registers cwd, and recently-touched sessions
       across reloads).
    3. ``done`` — otherwise.
    """
    if project_path is None:
        return "done"
    if project_activity(project_path).claude_running:
        return "live"
    if last_activity_epoch is not None:
        if (time.time() - last_activity_epoch) < LIVE_MTIME_WINDOW_SEC:
            return "live"
    return "done"


def _safe_cwd(proc: psutil.Process) -> Optional[str]:
    try:
        return proc.cwd()
    except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
        return None
