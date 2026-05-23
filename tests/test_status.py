"""Tests for the canonical status vocabulary."""
from __future__ import annotations

import pytest

from unwind.status import from_raw, is_done, merge


class TestFromRaw:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("complete", "done"),
            ("Complete", "done"),
            ("done", "done"),
            ("yielded", "yield"),
            ("yield", "yield"),
            ("running", "live"),
            ("in_progress", "live"),
            ("pending", "live"),
            ("live", "live"),
            ("failed", "failed"),
            ("error", "failed"),
        ],
    )
    def test_maps_callstack_vocab_to_canonical(self, raw, expected):
        assert from_raw(raw) == expected

    def test_unknown_or_empty_returns_none(self):
        assert from_raw(None) is None
        assert from_raw("") is None
        assert from_raw("totally_unknown") is None


class TestMerge:
    def test_empty_iterable_returns_done(self):
        assert merge([]) == "done"

    def test_all_none_returns_done(self):
        assert merge([None, None]) == "done"

    def test_live_beats_yield(self):
        """The key resolution: a live descendant must surface even when a
        yielded sibling exists. Without this the parent's rail would show
        ``yield`` despite work still happening below."""
        assert merge(["yield", "live"]) == "live"
        assert merge(["live", "yield"]) == "live"

    def test_yield_beats_failed_and_done(self):
        assert merge(["done", "yield"]) == "yield"
        assert merge(["failed", "yield"]) == "yield"

    def test_failed_beats_done(self):
        assert merge(["done", "failed"]) == "failed"

    def test_none_entries_ignored(self):
        assert merge(["live", None, "done"]) == "live"
        assert merge([None, "yield", None]) == "yield"


class TestIsDone:
    def test_only_live_is_in_flight(self):
        """Yield and failed both drop the row's in-progress dots — from
        the parent's perspective the child has returned control."""
        assert is_done("done") is True
        assert is_done("yield") is True
        assert is_done("failed") is True
        assert is_done("live") is False

    def test_none_propagates(self):
        assert is_done(None) is None
