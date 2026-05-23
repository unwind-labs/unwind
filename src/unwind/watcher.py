"""Filesystem watcher that translates disk events into typed ``Event`` records.

One watcher per project slug. Observes two directories:

- ``~/.claude/projects/<slug>/`` — per-session JSONLs
- ``<project>/.claude/callstack/log/`` — callstack invocation reports

Events are coalesced with a short debounce so a burst of JSONL appends during
a single turn becomes one ``messages_appended`` notification. Tail reading is
byte-offset based so we don't re-parse the whole file on each tick.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer  # type: ignore[import-untyped]

_Observer: Any = Observer  # factory; concrete type varies by platform

from .events import Event, EventBus
from .jsonl import iter_lines_from
from .messages import normalize_records
from .projects import invalidate_jsonl_listing, project_jsonl_listing
from .registry import callstack_for_slug, index_for_slug


log = logging.getLogger("unwind.watcher")


DEBOUNCE_SEC = 0.20
SESSION_UPDATE_COALESCE_SEC = 1.5


class _SessionUpdateCoalescer:
    """Rate-limit ``session_updated`` emissions per session.

    During a busy turn the watcher flushes 5-10×/sec; the frontend re-sorts
    the session list on every ``session_updated``. We keep ``messages_appended``
    at full rate (frontend needs the delta) but emit ``session_updated`` at
    most once per ``window`` seconds per session. A trailing-edge timer
    ensures the final post-burst summary always lands.
    """

    def __init__(self, emit, window: float = SESSION_UPDATE_COALESCE_SEC) -> None:
        self._emit = emit  # called as emit(session_id, payload)
        self._window = window
        self._last_emit: dict[str, float] = {}
        self._pending: dict[str, dict] = {}
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def request(self, session_id: str, payload: dict) -> None:
        now = time.monotonic()
        fire_now = False
        with self._lock:
            last = self._last_emit.get(session_id, 0.0)
            elapsed = now - last
            if elapsed >= self._window:
                self._last_emit[session_id] = now
                self._pending.pop(session_id, None)
                fire_now = True
            else:
                self._pending[session_id] = payload
                if session_id not in self._timers:
                    delay = max(0.0, self._window - elapsed)
                    timer = threading.Timer(
                        delay, self._fire_pending, args=(session_id,)
                    )
                    timer.daemon = True
                    self._timers[session_id] = timer
                    timer.start()
        if fire_now:
            try:
                self._emit(session_id, payload)
            except Exception:
                log.exception("session_updated emit failed")

    def _fire_pending(self, session_id: str) -> None:
        with self._lock:
            payload = self._pending.pop(session_id, None)
            self._timers.pop(session_id, None)
            if payload is not None:
                self._last_emit[session_id] = time.monotonic()
        if payload is not None:
            try:
                self._emit(session_id, payload)
            except Exception:
                log.exception("session_updated emit failed (trailing)")

    def stop(self) -> None:
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
            self._pending.clear()
        for t in timers:
            t.cancel()


class _DebouncedHandler(FileSystemEventHandler):
    """Batches filesystem events and invokes a callback after a quiet window.

    One long-lived flush thread per handler. on_any_event just adds the
    path to a set and signals the thread; the thread re-arms its
    DEBOUNCE_SEC quiet window each time a new event fires, and only
    invokes ``flush`` after a full window with no activity. Replaces an
    earlier ``threading.Timer``-per-burst scheme that churned threads
    on heavy turns.
    """

    def __init__(self, flush) -> None:
        self._flush = flush
        self._dirty: set[str] = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="unwind-watcher-flush", daemon=True
        )
        self._thread.start()

    def on_any_event(self, event: FileSystemEvent) -> None:
        if event.is_directory:
            return
        path = event.src_path
        if not isinstance(path, str):
            return
        with self._lock:
            self._dirty.add(path)
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while True:
            self._wake.wait()
            if self._stop.is_set():
                return
            # Drain bursts: keep extending the quiet window until
            # DEBOUNCE_SEC elapses without a new event.
            while True:
                self._wake.clear()
                if self._wake.wait(timeout=DEBOUNCE_SEC):
                    if self._stop.is_set():
                        return
                    continue
                break  # quiet
            self._do_flush()

    def _do_flush(self) -> None:
        with self._lock:
            paths = set(self._dirty)
            self._dirty.clear()
        if paths:
            try:
                self._flush(paths)
            except Exception:
                log.exception("watcher flush failed")


class ProjectWatcher:
    """Watches one project's JSONL dir and its callstack log dir."""

    def __init__(self, slug: str, bus: EventBus) -> None:
        self._slug = slug
        self._bus = bus
        self._observer: Any = None
        self._known_sessions: set[str] = set()
        self._offsets: dict[str, int] = {}  # session_id -> file bytes seen
        self._last_size: dict[str, int] = {}
        self._started = False
        self._su_coalescer = _SessionUpdateCoalescer(self._emit_session_updated)

    def _emit_session_updated(self, session_id: str, payload: dict) -> None:
        self._bus.publish_threadsafe(
            Event(
                type="session_updated",
                slug=self._slug,
                session_id=session_id,
                payload=payload,
            )
        )

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        index = index_for_slug(self._slug)
        project_dir = index.paths.project_dir
        callstack_dir = index.paths.callstack_log_dir

        # Seed known state so we don't re-emit historical messages as new.
        if project_dir.is_dir():
            for entry in project_jsonl_listing(project_dir):
                self._known_sessions.add(entry.sid)
                self._offsets[entry.sid] = entry.size
                self._last_size[entry.sid] = entry.size

        self._observer = _Observer()
        handler = _DebouncedHandler(self._handle_paths)
        self._handler = handler

        if project_dir.is_dir():
            self._observer.schedule(handler, str(project_dir), recursive=False)
            log.info("watching %s", project_dir)
        else:
            log.info("project dir not present yet: %s", project_dir)

        if callstack_dir.is_dir():
            self._observer.schedule(handler, str(callstack_dir), recursive=True)
            log.info("watching %s", callstack_dir)

        self._observer.start()

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2.0)
            self._observer = None
            self._started = False
        handler = getattr(self, "_handler", None)
        if handler is not None:
            handler.stop()
            self._handler = None
        self._su_coalescer.stop()

    # --- event routing ---------------------------------------------------

    def _handle_paths(self, paths: set[str]) -> None:
        touched_jsonls: list[Path] = []
        callstack_dirty = False
        index = index_for_slug(self._slug)
        project_dir = index.paths.project_dir
        callstack_dir = index.paths.callstack_log_dir

        for raw in paths:
            p = Path(raw)
            if p.suffix == ".jsonl" and p.parent == project_dir:
                touched_jsonls.append(p)
            elif callstack_dir.is_dir() and callstack_dir in p.parents:
                callstack_dirty = True

        if touched_jsonls:
            # The shared listing cache stat-snapshots mtime/size at scan time;
            # bust it so downstream consumers (status checks, ETag, fork
            # detector) see fresh stats after this write.
            invalidate_jsonl_listing(project_dir)

        for jsonl in touched_jsonls:
            self._handle_jsonl(jsonl)

        if callstack_dirty:
            self._handle_callstack()

    def _handle_jsonl(self, path: Path) -> None:
        """Dispatch a JSONL change into the right per-event handler.

        State machine (per session):
          * unknown → ``_handle_new_session``: cold path, full parse,
            session_created event.
          * known + shrank → ``_handle_shrink``: rotation/rewrite,
            invalidate the cache and re-read from offset 0.
          * known + grew → ``_handle_grew``: byte-offset tail read,
            messages_appended + coalesced session_updated events.

        ``unchanged`` (mtime fired but size identical) silently no-ops.
        """
        session_id = path.stem
        index = index_for_slug(self._slug)

        try:
            st = path.stat()
        except OSError:
            return
        size = st.st_size
        mtime = st.st_mtime
        prev_offset = self._offsets.get(session_id, 0)

        is_new = session_id not in self._known_sessions
        if is_new:
            self._known_sessions.add(session_id)
            self._handle_new_session(index, path, session_id, size, mtime)
            return

        if size < prev_offset:
            self._handle_shrink(index, session_id)
            prev_offset = 0
        if size > prev_offset:
            self._handle_grew(
                index, path, session_id, prev_offset, size, mtime
            )

    def _handle_new_session(
        self,
        index,
        path: Path,
        session_id: str,
        size: int,
        mtime: float,
    ) -> None:
        """Cold start: a JSONL appeared for an unknown session.

        Reads everything from offset 0, parses via the full index path
        (cache miss is unavoidable here), and emits ``session_created``
        + ``messages_appended`` + the coalesced ``session_updated``.
        """
        records, new_offset = self._tail_from(path, 0, size)
        self._offsets[session_id] = new_offset
        self._last_size[session_id] = size

        summary = index.get_session(session_id)
        self._bus.publish_threadsafe(
            Event(
                type="session_created",
                slug=self._slug,
                session_id=session_id,
                payload={"summary": _summary_dict(summary)},
            )
        )
        self._emit_tail(session_id, records, new_offset)
        # Recompute summary post-tail for a consistent timestamp.
        if records:
            updated = index.apply_increment(
                session_id, records, size, mtime
            ) or index.get_session(session_id)
            if updated is not None:
                self._su_coalescer.request(
                    session_id, {"summary": _summary_dict(updated)}
                )

    def _handle_shrink(self, index, session_id: str) -> None:
        """File rotated/rewrote — invalidate the cache. Next handler call
        will re-read from offset 0 because we drop the recorded offset."""
        index.invalidate(session_id)
        self._offsets[session_id] = 0
        self._last_size[session_id] = 0

    def _handle_grew(
        self,
        index,
        path: Path,
        session_id: str,
        prev_offset: int,
        size: int,
        mtime: float,
    ) -> None:
        """Warm path: a known JSONL grew. Byte-offset tail read, emit
        ``messages_appended`` + coalesced ``session_updated``."""
        records, new_offset = self._tail_from(path, prev_offset, size)
        self._offsets[session_id] = new_offset
        self._last_size[session_id] = size

        if not records:
            return
        self._emit_tail(session_id, records, new_offset)
        summary = index.apply_increment(
            session_id, records, size, mtime
        ) or index.get_session(session_id)
        if summary is not None:
            self._su_coalescer.request(
                session_id, {"summary": _summary_dict(summary)}
            )

    def _tail_from(
        self, path: Path, start_offset: int, size: int
    ) -> tuple[list[dict], int]:
        """Read every JSONL record from ``start_offset`` to EOF, in
        bounded-memory chunks. Returns (records, new_offset).

        ``iter_lines_from`` caps each read at MAX_TICK_READ_BYTES to
        bound memory; loop here so a single huge append still drains
        within one flush rather than stalling until the next FS event.
        """
        records: list[dict] = []
        new_offset = start_offset
        while True:
            chunk_records, next_offset = iter_lines_from(path, new_offset)
            if next_offset == new_offset:
                break
            records.extend(chunk_records)
            new_offset = next_offset
            if new_offset >= size:
                break
        return records, new_offset

    def _emit_tail(
        self, session_id: str, records: list[dict], new_offset: int
    ) -> None:
        """Normalize ``records`` and publish a ``messages_appended`` event.
        No-op when ``records`` is empty or yields zero normalized messages."""
        if not records:
            return
        msgs = normalize_records(records, include_meta=False)
        if not msgs:
            return
        self._bus.publish_threadsafe(
            Event(
                type="messages_appended",
                slug=self._slug,
                session_id=session_id,
                payload={
                    "messages": [m.to_dict() for m in msgs],
                    "file_offset": new_offset,
                },
            )
        )

    def _handle_callstack(self) -> None:
        ci = callstack_for_slug(self._slug)
        # Refresh internal cache by reading all reports (cheap — cached by mtime).
        ci.all_reports()
        self._bus.publish_threadsafe(
            Event(type="tree_changed", slug=self._slug, payload={})
        )


def _summary_dict(summary) -> dict | None:
    if summary is None:
        return None
    return {
        "session_id": summary.session_id,
        "title": summary.title,
        "first_timestamp": summary.first_timestamp.isoformat()
        if summary.first_timestamp
        else None,
        "last_timestamp": summary.last_timestamp.isoformat()
        if summary.last_timestamp
        else None,
        "message_count": summary.message_count,
        "cwd": summary.cwd,
        "git_branch": summary.git_branch,
    }


# --- per-process watcher manager -----------------------------------------


_watchers: dict[str, ProjectWatcher] = {}
_watchers_lock = threading.Lock()


def ensure_watcher(slug: str, bus: EventBus) -> ProjectWatcher:
    with _watchers_lock:
        existing = _watchers.get(slug)
        if existing is not None:
            return existing
        w = ProjectWatcher(slug, bus)
        _watchers[slug] = w
    w.start()
    # Also drip a small keepalive/bootstrap event so a fresh subscriber learns
    # the current sessions list (stale-while-revalidate is the UI's job but
    # this eliminates a race with a just-started Claude session).
    time.sleep(0)
    return w


def stop_all_watchers() -> None:
    with _watchers_lock:
        watchers = list(_watchers.values())
        _watchers.clear()
    for w in watchers:
        w.stop()


def stop_watcher(slug: str) -> None:
    """Stop and forget the watcher for one slug, if any."""
    with _watchers_lock:
        w = _watchers.pop(slug, None)
    if w is not None:
        w.stop()
