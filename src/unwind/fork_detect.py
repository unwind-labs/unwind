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

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING, TypeAlias

from ._cache import PathCache
from .jsonl import file_birth_ts, iter_lines
from .projects import project_jsonl_listing

if TYPE_CHECKING:
    from .session_scan import SessionScan


# Signature for the optional per-session scan accessor. Production wires
# ``CanvasTreeBuilder.get_scan`` (mtime-cached); tests can pass ``None``.
SessionScanner: TypeAlias = "Callable[[str], Optional[SessionScan]]"


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

# The sentinel the callstack runtime injects into a forked child's first
# queued message. See agent-callstack/agent_callstack/protocol.py
# (``FORK_SYSTEM_INSTRUCTION``).
#
# It is NOT at offset 0 of the content: ``protocol.starting_prompt`` prepends a
# ``"## Starting Task [<id>]\n\n"`` header before the instruction, so a real
# child's first enqueue reads ``"## Starting Task [a5ba828c]\n\nYou are running
# in a forked session — …"``. We therefore look for the sentinel ANYWHERE in
# the content rather than only as a prefix — an earlier ``startswith`` check
# silently classified every real fork as a non-fork once that header landed.
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

    def __init__(
        self,
        project_dir: Path,
        *,
        session_scanner: Optional[SessionScanner] = None,
    ) -> None:
        self._project_dir = project_dir
        self._lock = threading.Lock()
        self._probe_cache = PathCache(_build_probe_from_path)
        self._probes: dict[str, _Probe] = {}
        # ``session_scanner(sid) -> SessionScan`` from canvas_tree. Wired
        # via registry.fork_detector_for_slug so divergence-text lookup
        # reads from the canonical mtime-cached scan instead of doing a
        # separate per-fork walk. Optional so tests can omit it.
        self._scanner = session_scanner
        # Cache of the family root's uuid set (root_sid -> set[uuid]).
        # Used by divergence_text_for's fallback path; collect_uuids is
        # not free, so memoize per refresh signature.
        self._root_uuids_cache: dict[tuple[str, tuple], set[str]] = {}
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
        """Return True/False if known, None if we have no data on this session.

        ``fork_session_ids`` calls ``_refresh`` itself; we therefore decide
        "known vs unknown" against the POST-refresh probe set, not the
        stale instance attribute. Pre-refresh, ``_probes`` is empty on a
        fresh detector even when the project on disk has plenty of forks.
        """
        ids = self.fork_session_ids()
        with self._lock:
            if session_id not in self._probes:
                return None
        return session_id in ids

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
        """Return the label identifying this fork's assigned task.

        Reads from the canonical :class:`canvas_tree.SessionScan` (mtime/
        size-cached). Two priority sources:

        1. ``queue_op_starting_task`` — captured by ``scan_session``
           from the fork's first ``queue-operation`` record. The
           callstack runtime writes this when it spawns the fork, so
           it's the most reliable signal even when uuids are inherited.
        2. ``first_user_texts`` filtered against the family root's uuid
           set — fallback for forks that didn't go through callstack's
           runtime (manual ``claude --fork-session``). Returns the text
           of the first user message whose uuid ISN'T inherited.

        Returns ``None`` when no scanner is wired (tests) or when the
        fork has no recognizable divergence signal.
        """
        if self._scanner is None:
            return None
        scan = self._scanner(session_id)
        if scan is None:
            return None
        if scan.queue_op_starting_task:
            return scan.queue_op_starting_task
        # Fallback: first user message whose uuid is NOT inherited from
        # the family root. Filter the cached user-prefix list.
        root_uuids = self._family_root_uuids(session_id)
        if root_uuids is None:
            return None
        for uuid, text in scan.first_user_texts:
            if uuid not in root_uuids:
                return text or None
        return None

    def birth_ts(self, session_id: str) -> Optional[float]:
        """Return the JSONL birth timestamp for ``session_id``, or ``None``
        if we have no probe for it. Backed by the per-project probe cache
        (one ``os.stat`` per session across the project's lifetime)."""
        self._refresh()
        with self._lock:
            probe = self._probes.get(session_id)
            return probe.birth_ts if probe is not None else None

    def find_session_by_divergence_text(
        self, root_session_id: str, task: str
    ) -> Optional[str]:
        """Among children of ``root_session_id``, find the one whose first
        divergent user message matches ``task`` (e.g. ``/task-c``).

        Used to resolve in-flight tree rows whose ``session_id`` hasn't yet
        been written to ``report.yaml`` by the callstack plugin.
        """
        candidates = self.children_of(root_session_id)
        target = (task or "").strip()
        if not target:
            return None
        for sid in candidates:
            text = self.divergence_text_for(sid)
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

    def _family_root_uuids(self, fork_sid: str) -> Optional[set[str]]:
        """Return the family root's uuid set, memoized by refresh signature.

        Used by ``divergence_text_for``'s fallback path. The cache key
        includes ``_last_signature`` so any change to the project's
        JSONLs invalidates the entry. Returns ``None`` if the root
        can't be resolved or its JSONL is missing.
        """
        from .jsonl import collect_uuids

        root = self.family_root(fork_sid)
        if root is None:
            return None
        with self._lock:
            sig = self._last_signature
            key = (root, sig)
            cached = self._root_uuids_cache.get(key)
            if cached is not None:
                return cached
        root_path = self._project_dir / f"{root}.jsonl"
        if not root_path.is_file():
            return None
        uuids = collect_uuids(root_path)
        with self._lock:
            # Drop stale entries from previous signatures to bound memory.
            for stale_key in list(self._root_uuids_cache.keys()):
                if stale_key[1] != sig:
                    self._root_uuids_cache.pop(stale_key, None)
            self._root_uuids_cache[key] = uuids
        return uuids

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
            if isinstance(content, str) and _CALLSTACK_FORK_PROLOGUE in content:
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


