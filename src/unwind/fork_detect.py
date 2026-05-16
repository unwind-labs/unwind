"""Heuristic fork detection from JSONL content alone.

The callstack plugin writes ``report.yaml`` only when an invocation completes,
so during in-flight ``/call`` work we can't rely on it to classify newly-spawned
sessions. Fortunately ``claude --fork-session`` copies the parent's JSONL
verbatim into the new session's file, including the very first message uuid.

Marker-only classification: a session is a fork iff its first ``queue-operation``
enqueue begins with the callstack runtime's fork-prologue ("You are running in a
forked session..."). Sessions that merely share a head uuid via ``claude
--resume`` (or via ``--fork-session`` invoked outside callstack) are NOT
classified as forks — they remain visible in the top-level session list. This
trades a small amount of duplication (a non-callstack fork shows up as its own
root) for a much lower false-positive rate, which would otherwise hide many
legitimate independent runs whose first user message happens to be identical.

This is run on demand and cached by JSONL (mtime, size).
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ._cache import PathCache
from .jsonl import _text_blocks, file_birth_ts, iter_lines
from .projects import project_jsonl_listing


# How many leading uuids to sample from each JSONL.
PROBE_N = 5

# Skip _refresh entirely within this TTL of the last successful refresh.
# A GET /sessions response calls into the detector 3-4× per request; this
# collapses them to one filesystem pass.
_REFRESH_TTL_SECONDS = 1.0

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


def _build_probe_from_path(path: Path) -> Optional[_Probe]:
    try:
        stat = path.stat()
    except OSError:
        return None
    return _build_probe(path, stat.st_mtime, stat.st_size)


class ForkDetector:
    """Per-project heuristic fork classifier."""

    def __init__(self, project_dir: Path) -> None:
        self._project_dir = project_dir
        self._lock = threading.Lock()
        self._probe_cache = PathCache(_build_probe_from_path)
        self._probes: dict[str, _Probe] = {}
        # divergence_text is computed once per fork session and never changes
        # (it's the assigned task name from the queue-operation record). Keep
        # it in a separate map that survives ``_refresh``.
        self._divergence_text: dict[str, Optional[str]] = {}
        self._divergence_resolved: set[str] = set()
        self._last_refresh_ts: float = 0.0
        self._last_signature: tuple = ()
        self._last_dir_mtime: float = -1.0

    def fork_session_ids(self) -> set[str]:
        """Return the set of session_ids classified as forks.

        Only callstack-prologue-marked sessions count. Sharing a head uuid
        with another session is NOT sufficient — that catches ``claude
        --resume`` continuations and any project where multiple independent
        runs happen to begin with the same first message.
        """
        self._refresh()
        with self._lock:
            return {
                sid
                for sid, probe in self._probes.items()
                if probe.is_callstack_fork
            }

    def is_fork(self, session_id: str) -> Optional[bool]:
        """Return True/False if known, None if we have no data on this session."""
        return session_id in self.fork_session_ids() if self._probes else None

    def family_root(self, session_id: str) -> Optional[str]:
        """Return the canonical root of ``session_id``'s family, or None.

        With marker-only fork classification, the "root" is the single
        unmarked member of the family (the parent that did the ``/call``).
        Returns ``None`` if ``session_id`` is unknown or no unmarked
        sibling exists.
        """
        self._refresh()
        with self._lock:
            probe = self._probes.get(session_id)
            if probe is None or probe.head is None:
                return None
            head = probe.head
            unmarked = [
                sid
                for sid, p in self._probes.items()
                if p.head == head and not p.is_callstack_fork
            ]
            if not unmarked:
                return None
            unmarked.sort(key=lambda sid: (self._probes[sid].birth_ts, sid))
            return unmarked[0]

    def children_of(self, session_id: str) -> list[str]:
        """Return fork session_ids whose root is ``session_id``.

        Only marker-bearing siblings count as forks (see
        ``fork_session_ids``). ``session_id`` is treated as the family root
        if it shares the head uuid with at least one marked sibling and
        is itself unmarked.
        """
        self._refresh()
        out: list[str] = []
        with self._lock:
            target = self._probes.get(session_id)
            if target is None or target.head is None:
                return out
            if target.is_callstack_fork:
                # A fork can't be the root of other forks in this scheme.
                return out
            head = target.head
            marked = [
                sid
                for sid, p in self._probes.items()
                if p.head == head and p.is_callstack_fork
            ]
            marked.sort(key=lambda sid: (self._probes[sid].birth_ts, sid))
            return marked

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
        now = time.monotonic()
        try:
            dir_mtime = self._project_dir.stat().st_mtime
        except OSError:
            with self._lock:
                self._last_refresh_ts = now
            return
        # Fast path: TTL window AND directory contents unchanged
        # (new/removed JSONL files bump dir mtime; in-place growth does not,
        # but a 1-second staleness window is acceptable for that case).
        with self._lock:
            if (
                now - self._last_refresh_ts < _REFRESH_TTL_SECONDS
                and dir_mtime == self._last_dir_mtime
            ):
                return
        # Shared listing: one os.scandir pass per request, cached by dir mtime.
        listing = project_jsonl_listing(self._project_dir)
        signature = tuple((e.path.name, e.mtime, e.size) for e in listing)
        with self._lock:
            if signature == self._last_signature:
                self._last_refresh_ts = now
                self._last_dir_mtime = dir_mtime
                return
        new_probes: dict[str, _Probe] = {}
        for entry in listing:
            probe = self._probe_cache.get(entry.path)
            if probe is not None:
                new_probes[entry.sid] = probe
        with self._lock:
            self._probes = new_probes
            self._last_signature = signature
            self._last_refresh_ts = now
            self._last_dir_mtime = dir_mtime


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
    birth = file_birth_ts(path, fallback=mtime)
    return _Probe(
        first_uuids=uuids,
        birth_ts=birth,
        mtime=mtime,
        size=size,
        is_callstack_fork=is_callstack_fork,
    )


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
        fallback_user_text = _text_blocks(rec.get("message"), " ")

    return queue_text or fallback_user_text
