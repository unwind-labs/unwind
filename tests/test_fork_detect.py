"""Tests for the heuristic fork detector."""
import json
import time
from pathlib import Path

from unwind.fork_detect import ForkDetector


def _write(p: Path, lines: list[dict]) -> None:
    p.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_lone_session_is_not_a_fork(tmp_path: Path):
    s = tmp_path / "one.jsonl"
    _write(s, [{"uuid": "u-head", "timestamp": "2026-04-24T10:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == set()


def test_two_sessions_sharing_head_uuid(tmp_path: Path):
    # parent (older) — first uuid u-head
    p = tmp_path / "parent.jsonl"
    _write(
        p,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-2", "timestamp": "2026-04-24T09:00:01Z", "type": "assistant"},
        ],
    )
    # fork (newer) — first uuid u-head (cloned from parent)
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T10:00:00Z", "type": "user"},
            {"uuid": "u-fork-own", "timestamp": "2026-04-24T10:00:01Z", "type": "assistant"},
        ],
    )
    fd = ForkDetector(tmp_path)
    forks = fd.fork_session_ids()
    assert forks == {"fork"}


def test_oldest_in_family_is_root_not_fork(tmp_path: Path):
    """When timestamps are equal, lexicographic session_id breaks the tie."""
    a = tmp_path / "AAA.jsonl"
    b = tmp_path / "BBB.jsonl"
    same_ts = "2026-04-24T10:00:00Z"
    _write(a, [{"uuid": "u-head", "timestamp": same_ts, "type": "user"}])
    _write(b, [{"uuid": "u-head", "timestamp": same_ts, "type": "user"}])
    fd = ForkDetector(tmp_path)
    forks = fd.fork_session_ids()
    assert "AAA" not in forks
    assert "BBB" in forks


def test_unrelated_sessions_dont_classify_as_forks(tmp_path: Path):
    a = tmp_path / "x.jsonl"
    b = tmp_path / "y.jsonl"
    _write(a, [{"uuid": "uA", "timestamp": "2026-04-24T09:00:00Z", "type": "user"}])
    _write(b, [{"uuid": "uB", "timestamp": "2026-04-24T10:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == set()


def test_cache_is_invalidated_on_growth(tmp_path: Path):
    s = tmp_path / "g.jsonl"
    _write(s, [{"uuid": "u1", "timestamp": "2026-04-24T09:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    fd.fork_session_ids()  # warm cache
    time.sleep(0.01)
    # Append a second session that should now be classified relative to s.
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [{"uuid": "u1", "timestamp": "2026-04-24T10:00:00Z", "type": "user"}],
    )
    forks = fd.fork_session_ids()
    assert forks == {"fork"}
