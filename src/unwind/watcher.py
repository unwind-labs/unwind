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
from .registry import callstack_for_slug, index_for_slug


log = logging.getLogger("unwind.watcher")


DEBOUNCE_SEC = 0.20


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

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        index = index_for_slug(self._slug)
        project_dir = index.paths.project_dir
        callstack_dir = index.paths.callstack_log_dir

        # Seed known state so we don't re-emit historical messages as new.
        if project_dir.is_dir():
            for jsonl in project_dir.glob("*.jsonl"):
                self._known_sessions.add(jsonl.stem)
                try:
                    self._offsets[jsonl.stem] = jsonl.stat().st_size
                    self._last_size[jsonl.stem] = jsonl.stat().st_size
                except OSError:
                    self._offsets[jsonl.stem] = 0

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

        for jsonl in touched_jsonls:
            self._handle_jsonl(jsonl)

        if callstack_dirty:
            self._handle_callstack()

    def _handle_jsonl(self, path: Path) -> None:
        session_id = path.stem
        index = index_for_slug(self._slug)

        is_new = session_id not in self._known_sessions
        if is_new:
            self._known_sessions.add(session_id)

        try:
            st = path.stat()
        except OSError:
            return
        size = st.st_size
        mtime = st.st_mtime
        prev = self._offsets.get(session_id, 0)

        # If file shrank (rotation / rewrite), reset offset and force a
        # full re-parse so summary fields don't drift.
        shrank = size < prev
        if shrank:
            prev = 0
            index.invalidate(session_id)

        records, new_offset = iter_lines_from(path, prev)
        self._offsets[session_id] = new_offset
        self._last_size[session_id] = size
        records_list = list(records)

        if is_new:
            # Cold start path: cache miss → full parse via get_session.
            summary = index.get_session(session_id)
            self._bus.publish_threadsafe(
                Event(
                    type="session_created",
                    slug=self._slug,
                    session_id=session_id,
                    payload={
                        "summary": _summary_dict(summary),
                    },
                )
            )

        # Tail: normalize and push as messages_appended.
        if records_list:
            msgs = normalize_records(records_list, include_meta=False)
            if msgs:
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
            # Always emit an updated summary when the JSONL grew.
            summary = index.apply_increment(
                session_id, records_list, size, mtime
            )
            if summary is None:
                # No cached entry (e.g. shrink-triggered invalidate, or the
                # session existed at startup but no one has listed yet) —
                # fall back to a full parse.
                summary = index.get_session(session_id)
            if summary is not None:
                self._bus.publish_threadsafe(
                    Event(
                        type="session_updated",
                        slug=self._slug,
                        session_id=session_id,
                        payload={"summary": _summary_dict(summary)},
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
