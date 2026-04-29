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


LIVE_MTIME_WINDOW_SEC = 300.0  # 5 minutes


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
            name = (info.get("name") or "").lower()
            cmdline = info.get("cmdline") or []
            if name == "claude" or any(
                isinstance(c, str) and c.endswith("/claude") for c in cmdline
            ):
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
