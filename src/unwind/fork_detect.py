"""Heuristic fork detection from JSONL content alone.

The callstack plugin writes ``report.yaml`` only when an invocation completes,
so during in-flight ``/call`` work we can't rely on it to classify newly-spawned
sessions. Fortunately ``claude --fork-session`` copies the parent's JSONL
verbatim into the new session's file, including the very first message uuid.

Two-tier classification, since "shares head uuid with an older session" is
necessary but not sufficient (``claude --resume`` also produces a new JSONL
with the parent's head uuid):

1. If a callstack runtime is in use, every spawned child's first
   ``queue-operation`` enqueue carries a fork-prologue ("You are running in a
   forked session..."). Only those marked sessions are classified as forks;
   unmarked siblings are session resumes and remain visible.
2. If no member of the family carries the marker (e.g. ``deep-rewrite`` runs
   that spawn ``claude --fork-session`` directly without callstack), fall back
   to the original heuristic: oldest member is the root, every other is a fork.

This is run on demand and cached by JSONL (mtime, size).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .jsonl import iter_lines


# How many leading uuids to sample from each JSONL.
PROBE_N = 5

# Number of leading records to scan for the callstack fork-prologue marker.
# The marker, when present, sits in the very first ``queue-operation`` enqueue
# (typically record 0); a small window keeps probing cheap.
PROBE_PROLOGUE_N = 4

# The exact prefix the callstack runtime injects into a forked child's first
# queued message. See agent-callstack/agent_callstack/protocol.py.
_CALLSTACK_FORK_PROLOGUE = "You are running in a forked session"


@dataclass
class _Probe:
    first_uuids: list[str]
    birth_ts: float
    mtime: float
    size: int
    # True iff the JSONL begins with a callstack-runtime fork prologue. Lets
    # ``fork_session_ids`` distinguish true ``/call`` children from sessions
    # that merely share a head uuid via ``claude --resume``.
    is_callstack_fork: bool = False

    @property
    def head(self) -> Optional[str]:
        return self.first_uuids[0] if self.first_uuids else None


class ForkDetector:
    """Per-project heuristic fork classifier."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._lock = threading.Lock()
        self._probes: dict[str, _Probe] = {}
        # divergence_text is computed once per fork session and never changes
        # (it's the assigned task name from the queue-operation record). Keep
        # it in a separate map that survives ``_refresh``.
        self._divergence_text: dict[str, Optional[str]] = {}
        self._divergence_resolved: set[str] = set()

    def fork_session_ids(self) -> set[str]:
        """Return the set of session_ids classified as forks (non-root in their family)."""
        self._refresh()
        out: set[str] = set()
        with self._lock:
            families = self._families_locked()
            for head, members in families.items():
                if len(members) <= 1 or not head:
                    continue
                # If any family member has the callstack fork-prologue marker,
                # only the marked sessions are true ``/call`` children; the
                # rest are ``claude --resume`` continuations that just happen
                # to share the head uuid and should remain visible.
                marked = [s for s in members if self._probes[s].is_callstack_fork]
                if marked:
                    out.update(marked)
                    continue
                # No markers anywhere — fall back to the file-birth-time
                # heuristic. Forks copy the parent's first record verbatim, so
                # the in-record timestamp is identical across the family;
                # ``st_birthtime`` is the only reliable ordering signal.
                members.sort(key=lambda sid: (self._probes[sid].birth_ts, sid))
                for sid in members[1:]:
                    out.add(sid)
        return out

    def is_fork(self, session_id: str) -> Optional[bool]:
        """Return True/False if known, None if we have no data on this session."""
        return session_id in self.fork_session_ids() if self._probes else None

    def family_root(self, session_id: str) -> Optional[str]:
        """Return the canonical root of ``session_id``'s family, or None."""
        self._refresh()
        with self._lock:
            probe = self._probes.get(session_id)
            if probe is None or probe.head is None:
                return None
            head = probe.head
            members = [
                sid for sid, p in self._probes.items() if p.head == head
            ]
            members.sort(key=lambda sid: (self._probes[sid].birth_ts, sid))
            return members[0] if members else None

    def children_of(self, session_id: str) -> list[str]:
        """Return fork session_ids whose root is ``session_id``."""
        self._refresh()
        out: list[str] = []
        with self._lock:
            target = self._probes.get(session_id)
            if target is None or target.head is None:
                return out
            head = target.head
            members = [
                sid for sid, p in self._probes.items() if p.head == head
            ]
            members.sort(key=lambda sid: (self._probes[sid].birth_ts, sid))
            if not members or members[0] != session_id:
                return out
            return members[1:]

    def divergence_text_for(self, session_id: str) -> Optional[str]:
        """Return the cached divergence text for a fork, if known."""
        with self._lock:
            return self._divergence_text.get(session_id)

    def find_session_by_divergence_text(
        self, root_session_id: str, task: str
    ) -> Optional[str]:
        """Among children of ``root_session_id``, find the one whose first
        divergent user message matches ``task`` (e.g. ``/task-c``).

        Used to resolve in-flight tree rows whose ``session_id`` hasn't yet
        been written to ``report.yaml`` by the callstack plugin.
        """
        self._enrich_divergence_for_root(root_session_id)
        candidates = self.children_of(root_session_id)
        target = (task or "").strip()
        if not target:
            return None
        with self._lock:
            for sid in candidates:
                text = self._divergence_text.get(sid)
                if text and text.strip() == target:
                    return sid
        return None

    # --- internals -------------------------------------------------------

    def _families_locked(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for sid, probe in self._probes.items():
            head = probe.head
            if head is None:
                continue
            out.setdefault(head, []).append(sid)
        return out

    def _enrich_divergence_for_root(self, root_session_id: str) -> None:
        """Compute ``divergence_text`` for every fork sharing this root.

        The divergence text is the first user message in the fork whose uuid
        is NOT in the root's uuid set — that's the prompt that started the
        fork's own work.
        """
        from .jsonl import collect_uuids

        with self._lock:
            target = self._probes.get(root_session_id)
            if target is None or target.head is None:
                return
            head = target.head
            members = [
                sid
                for sid, p in self._probes.items()
                if p.head == head and sid != root_session_id
            ]
            if not members:
                return
            pending = [s for s in members if s not in self._divergence_resolved]
            if not pending:
                return

        # Heavy I/O outside the lock.
        root_path = self._project_dir / f"{root_session_id}.jsonl"
        if not root_path.is_file():
            return
        root_uuids = collect_uuids(root_path)

        results: dict[str, Optional[str]] = {}
        for sid in pending:
            fork_path = self._project_dir / f"{sid}.jsonl"
            if not fork_path.is_file():
                results[sid] = None
                continue
            results[sid] = _first_divergent_user_text(fork_path, root_uuids)

        with self._lock:
            for sid, text in results.items():
                self._divergence_text[sid] = text
                self._divergence_resolved.add(sid)

    def _refresh(self) -> None:
        if not self._project_dir.is_dir():
            return
        with self._lock:
            existing = dict(self._probes)
        new_probes: dict[str, _Probe] = {}
        for jsonl in self._project_dir.glob("*.jsonl"):
            try:
                stat = jsonl.stat()
            except OSError:
                continue
            sid = jsonl.stem
            cached = existing.get(sid)
            if (
                cached is not None
                and cached.mtime == stat.st_mtime
                and cached.size == stat.st_size
            ):
                new_probes[sid] = cached
                continue
            probe = _build_probe(jsonl, stat.st_mtime, stat.st_size)
            new_probes[sid] = probe

        with self._lock:
            self._probes = new_probes


def _build_probe(path: Path, mtime: float, size: int) -> _Probe:
    uuids: list[str] = []
    is_callstack_fork = False
    scanned = 0
    for rec in iter_lines(path):
        scanned += 1
        if (
            not is_callstack_fork
            and rec.get("type") == "queue-operation"
            and rec.get("operation") == "enqueue"
        ):
            content = rec.get("content")
            if isinstance(content, str) and content.startswith(_CALLSTACK_FORK_PROLOGUE):
                is_callstack_fork = True
        u = rec.get("uuid")
        if isinstance(u, str):
            uuids.append(u)
        if len(uuids) >= PROBE_N and scanned >= PROBE_PROLOGUE_N:
            break
    birth = _file_birth_ts(path, fallback=mtime)
    return _Probe(
        first_uuids=uuids,
        birth_ts=birth,
        mtime=mtime,
        size=size,
        is_callstack_fork=is_callstack_fork,
    )


def _file_birth_ts(path: Path, fallback: float) -> float:
    """Return file creation time, with sensible fallbacks across platforms."""
    try:
        st = path.stat()
    except OSError:
        return fallback
    bt = getattr(st, "st_birthtime", None)
    if isinstance(bt, (int, float)) and bt > 0:
        return float(bt)
    # Linux: st_ctime is last metadata change, but on a newly created append-
    # only file it will be ≈ creation time and nothing rewrites the inode.
    return float(st.st_ctime)


_STARTING_TASK_RE = re.compile(
    r"##\s*Starting\s+Task[^\n]*\n+\s*(\S[^\n]*)", re.IGNORECASE
)


def _first_divergent_user_text(path: Path, ancestor_uuids: set[str]) -> Optional[str]:
    """Return a label identifying this fork's assigned task.

    Strategy in priority order:

    1. ``queue-operation`` records: callstack writes one of these as the
       fork's first action. Its ``content`` ends with
       ``## Starting Task [...] \\n\\n /task-X`` — that's our most reliable
       signal even when uuids are inherited (queue-operation records lack a
       ``uuid`` field, so they aren't filtered by the ancestor set).
    2. First ``user`` message whose uuid is NOT in the ancestor's uuid set —
       fallback for forks that didn't go through callstack's runtime.
    """
    queue_text: Optional[str] = None
    fallback_user_text: Optional[str] = None

    for rec in iter_lines(path):
        rtype = rec.get("type")

        if rtype == "queue-operation" and queue_text is None:
            content = rec.get("content")
            if isinstance(content, str):
                m = _STARTING_TASK_RE.search(content)
                if m:
                    queue_text = m.group(1).strip()
                    # First queue-op is enough — early return.
                    return queue_text
            continue

        if rtype != "user":
            continue
        if fallback_user_text is not None:
            continue
        u = rec.get("uuid")
        if not isinstance(u, str) or u in ancestor_uuids:
            continue
        msg = rec.get("message")
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        text: Optional[str] = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            for block in content:
                if (
                    isinstance(block, dict)
                    and block.get("type") == "text"
                    and isinstance(block.get("text"), str)
                ):
                    text = block["text"]
                    break
        fallback_user_text = text

    return queue_text or fallback_user_text
