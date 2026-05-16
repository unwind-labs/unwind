"""Coalescing of ``session_updated`` emissions.

The watcher fires the underlying flush 5-10x/sec during a busy turn. Per
T23, ``session_updated`` is rate-limited to one leading-edge emit plus one
trailing-edge emit per ~window per session, while ``messages_appended``
stays at full rate.
"""
from __future__ import annotations

import time

from unwind.watcher import _SessionUpdateCoalescer


def test_burst_of_ten_emits_at_most_two_session_updated() -> None:
    """10 rapid appends within one window emit exactly 2 events: leading + trailing."""
    emits: list[tuple[str, dict]] = []
    coalescer = _SessionUpdateCoalescer(
        lambda sid, payload: emits.append((sid, payload)), window=0.3
    )
    try:
        for i in range(10):
            coalescer.request("sess-a", {"summary": {"n": i}})
            time.sleep(0.02)  # 10 * 20ms = 200ms < 300ms window

        # Wait for trailing-edge timer.
        time.sleep(0.5)

        assert len(emits) <= 2, f"expected <=2 emits, got {len(emits)}: {emits}"
        assert len(emits) >= 1, "leading-edge emit missing"
        # Leading-edge fires with the first payload.
        assert emits[0][1]["summary"]["n"] == 0
        # Trailing-edge (if it fired) carries the LATEST payload.
        if len(emits) == 2:
            assert emits[1][1]["summary"]["n"] == 9, (
                f"trailing emit should carry latest payload, got {emits[1]}"
            )
    finally:
        coalescer.stop()


def test_emits_independently_per_session() -> None:
    """Two sessions each get their own leading + trailing emit; no cross-throttling."""
    emits: list[tuple[str, dict]] = []
    coalescer = _SessionUpdateCoalescer(
        lambda sid, payload: emits.append((sid, payload)), window=0.3
    )
    try:
        for i in range(5):
            coalescer.request("sess-a", {"summary": {"n": i}})
            coalescer.request("sess-b", {"summary": {"n": i}})
            time.sleep(0.02)

        time.sleep(0.5)

        a = [p for sid, p in emits if sid == "sess-a"]
        b = [p for sid, p in emits if sid == "sess-b"]
        assert 1 <= len(a) <= 2, f"sess-a emits: {a}"
        assert 1 <= len(b) <= 2, f"sess-b emits: {b}"
    finally:
        coalescer.stop()


def test_quiet_period_allows_immediate_emit() -> None:
    """After a quiet period > window, next request fires immediately again."""
    emits: list[tuple[str, dict]] = []
    coalescer = _SessionUpdateCoalescer(
        lambda sid, payload: emits.append((sid, payload)), window=0.15
    )
    try:
        coalescer.request("sess-a", {"summary": {"n": 0}})
        # Wait past the window with no further activity.
        time.sleep(0.3)
        before = len(emits)
        coalescer.request("sess-a", {"summary": {"n": 1}})
        # This request should emit immediately (leading-edge).
        assert len(emits) == before + 1
        assert emits[-1][1]["summary"]["n"] == 1
    finally:
        coalescer.stop()


def test_stop_cancels_pending_timers() -> None:
    """stop() prevents a queued trailing-edge emit from firing."""
    emits: list[tuple[str, dict]] = []
    coalescer = _SessionUpdateCoalescer(
        lambda sid, payload: emits.append((sid, payload)), window=0.5
    )
    coalescer.request("sess-a", {"summary": {"n": 0}})
    coalescer.request("sess-a", {"summary": {"n": 1}})  # suppressed, schedules timer
    assert len(emits) == 1
    coalescer.stop()
    time.sleep(0.7)  # well past window
    assert len(emits) == 1, "stop() should have cancelled the trailing timer"
