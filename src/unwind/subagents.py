"""Per-session subagent traces.

When the assistant uses the ``Agent`` (or ``Task``) tool, Claude Code spawns
a subagent in-process and persists its full trace alongside the session:

    ~/.claude/projects/<slug>/<session_id>/subagents/agent-<agentId>.jsonl
    ~/.claude/projects/<slug>/<session_id>/subagents/agent-<agentId>.meta.json

The .meta.json carries ``agentType`` and ``description``; the .jsonl is a
regular Claude conversation log. We surface these as additional children
in the session's call tree so the user can drill into each subagent's
internal turns.
"""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ._cache import PathCache
from .jsonl import EPOCH


SUBAGENT_PREFIX = "agent-"


@dataclass(frozen=True)
class SubagentInvocation:
    agent_id: str
    description: str
    agent_type: str
    jsonl_path: Path
    created_at: Optional[datetime]
    message_count: int

    @property
    def synthetic_session_id(self) -> str:
        """Prefixed id used by the frontend / messages endpoint to address this trace."""
        return f"{SUBAGENT_PREFIX}{self.agent_id}"


class SubagentIndex:
    """Caches per-session subagent invocations keyed by parent session_id."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._lock = threading.Lock()
        # Per-file (path, mtime, size) cache so touching one subagent JSONL
        # doesn't force re-parsing every other file in the same session.
        self._file_cache: PathCache = PathCache(self._build_one)
        # Per-session listing cache, keyed by the subagent dir's mtime.
        # Cheap fast-path for the common "nothing changed" case so repeated
        # list_for_session calls in one request don't re-glob.
        self._cache: dict[str, tuple[float, list[SubagentInvocation]]] = {}
        # Project-wide "which sids have a subagents/ dir" cache, keyed by
        # the project dir's mtime. Recomputed when a session is added or
        # removed (the only events that bump the dir mtime).
        self._parent_sids_cache: Optional[tuple[float, frozenset[str]]] = None

    def parent_sids(self) -> set[str]:
        """Return every parent session_id that has a ``subagents/`` directory.

        One ``os.scandir`` of the project dir, mtime-cached. Used by the
        spawn resolver to enumerate subagent-spawn parents without
        re-implementing the walk inside an unrelated module.
        """
        if not self._project_dir.is_dir():
            return set()
        try:
            dir_mtime = self._project_dir.stat().st_mtime
        except OSError:
            return set()

        with self._lock:
            cached = self._parent_sids_cache
            if cached is not None and cached[0] == dir_mtime:
                return set(cached[1])

        out: set[str] = set()
        try:
            with os.scandir(self._project_dir) as it:
                for entry in it:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    if os.path.isdir(os.path.join(entry.path, "subagents")):
                        out.add(entry.name)
        except OSError:
            return set()

        with self._lock:
            self._parent_sids_cache = (dir_mtime, frozenset(out))
        return out

    def list_for_session(self, session_id: str) -> list[SubagentInvocation]:
        sub_dir = self._project_dir / session_id / "subagents"
        if not sub_dir.is_dir():
            return []
        try:
            stat_mtime = sub_dir.stat().st_mtime
        except OSError:
            return []

        with self._lock:
            cached = self._cache.get(session_id)
            if cached is not None and cached[0] == stat_mtime:
                return list(cached[1])

        out: list[SubagentInvocation] = []
        for jsonl in sub_dir.glob("agent-*.jsonl"):
            inv = self._file_cache.get(jsonl)
            if inv is not None:
                out.append(inv)
        out.sort(key=lambda i: i.created_at or EPOCH)

        with self._lock:
            self._cache[session_id] = (stat_mtime, out)
        return out

    def resolve(self, synthetic_id: str) -> Optional[Path]:
        """Map ``agent-<agentId>`` back to the JSONL path."""
        if not synthetic_id.startswith(SUBAGENT_PREFIX):
            return None
        agent_id = synthetic_id[len(SUBAGENT_PREFIX):]
        if not self._project_dir.is_dir():
            return None
        # Search every session's subagents/ for a matching file.
        for sess_dir in self._project_dir.iterdir():
            if not sess_dir.is_dir():
                continue
            sub_dir = sess_dir / "subagents"
            if not sub_dir.is_dir():
                continue
            candidate = sub_dir / f"agent-{agent_id}.jsonl"
            if candidate.is_file():
                return candidate
        return None

    def get(self, synthetic_id: str) -> Optional[SubagentInvocation]:
        path = self.resolve(synthetic_id)
        if path is None:
            return None
        return self._file_cache.get(path)

    # --- internals --------------------------------------------------------

    def _build_one(self, jsonl: Path) -> Optional[SubagentInvocation]:
        agent_id = jsonl.stem.removeprefix(SUBAGENT_PREFIX)
        meta_path = jsonl.with_suffix(".meta.json")
        description = ""
        agent_type = ""
        if meta_path.is_file():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                if isinstance(meta, dict):
                    description = str(meta.get("description") or "").strip()
                    agent_type = str(meta.get("agentType") or "").strip()
            except (OSError, json.JSONDecodeError):
                pass

        # Cheap walk of the JSONL: count user/assistant lines, capture first ts.
        msg_count = 0
        first_ts: Optional[datetime] = None
        try:
            with jsonl.open("r", encoding="utf-8", errors="replace") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        rec = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    rtype = rec.get("type")
                    if rtype in ("user", "assistant"):
                        msg_count += 1
                    if first_ts is None:
                        ts_raw = rec.get("timestamp")
                        if isinstance(ts_raw, str):
                            try:
                                ts_norm = ts_raw[:-1] + "+00:00" if ts_raw.endswith("Z") else ts_raw
                                first_ts = datetime.fromisoformat(ts_norm).astimezone(timezone.utc)
                            except ValueError:
                                pass
        except OSError:
            return None

        if first_ts is None:
            try:
                bt = getattr(jsonl.stat(), "st_birthtime", None)
                if isinstance(bt, (int, float)) and bt > 0:
                    first_ts = datetime.fromtimestamp(bt, tz=timezone.utc)
            except OSError:
                pass

        return SubagentInvocation(
            agent_id=agent_id,
            description=description or f"Agent {agent_id[:8]}",
            agent_type=agent_type or "agent",
            jsonl_path=jsonl,
            created_at=first_ts,
            message_count=msg_count,
        )
