"""Single-thread debounce in the watcher.

Verifies a burst of events results in exactly one flush, and that the
flush thread shuts down cleanly on stop().
"""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from unwind.watcher import _DebouncedHandler


def _fs_event(path: str):
    return SimpleNamespace(is_directory=False, src_path=path)


def test_burst_coalesces_to_single_flush():
    calls: list[set[str]] = []
    flushed = threading.Event()

    def flush(paths: set[str]) -> None:
        calls.append(set(paths))
        flushed.set()

    h = _DebouncedHandler(flush)
    try:
        # Fire 5 events within the debounce window.
        for i in range(5):
            h.on_any_event(_fs_event(f"/tmp/x{i}.jsonl"))
            time.sleep(0.02)

        # Wait for the flush to fire (DEBOUNCE_SEC default 0.20s).
        assert flushed.wait(timeout=2.0), "flush never fired"
        assert len(calls) == 1, f"expected one flush, got {len(calls)}: {calls}"
        assert calls[0] == {f"/tmp/x{i}.jsonl" for i in range(5)}
    finally:
        h.stop()


def test_handler_uses_single_long_lived_thread():
    """Multiple bursts must NOT spawn multiple flush threads."""
    flush_count = 0

    def flush(paths: set[str]) -> None:
        nonlocal flush_count
        flush_count += 1

    h = _DebouncedHandler(flush)
    try:
        thread_at_start = h._thread
        assert thread_at_start.is_alive()

        # Three separate bursts.
        for burst in range(3):
            h.on_any_event(_fs_event(f"/tmp/b{burst}.jsonl"))
            time.sleep(0.3)  # > DEBOUNCE_SEC

        assert flush_count == 3
        # Same thread served all bursts.
        assert h._thread is thread_at_start
    finally:
        h.stop()
        # After stop, the thread should exit.
        h._thread.join(timeout=2)
        assert not h._thread.is_alive()
