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


def test_two_sessions_sharing_head_uuid_without_marker(tmp_path: Path):
    """Sharing a head uuid is no longer sufficient to classify as a fork —
    the callstack runtime's fork prologue must be present. This protects
    against false positives when independent runs happen to begin with the
    same first message, or when ``claude --resume`` clones a parent.
    """
    p = tmp_path / "parent.jsonl"
    _write(
        p,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-2", "timestamp": "2026-04-24T09:00:01Z", "type": "assistant"},
        ],
    )
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T10:00:00Z", "type": "user"},
            {"uuid": "u-fork-own", "timestamp": "2026-04-24T10:00:01Z", "type": "assistant"},
        ],
    )
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == set()


def test_marked_sibling_is_classified_as_fork(tmp_path: Path):
    """The same shared-head-uuid pair, but with the callstack fork
    prologue present in the child, IS a fork.
    """
    p = tmp_path / "parent.jsonl"
    _write(
        p,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
        ],
    )
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "You are running in a forked session — execute /task-a",
                "timestamp": "2026-04-24T10:00:00Z",
            },
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-fork-own", "timestamp": "2026-04-24T10:00:01Z", "type": "user"},
        ],
    )
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == {"fork"}


def test_fork_prologue_behind_starting_task_header(tmp_path: Path):
    """Regression: the runtime prepends a ``## Starting Task [<id>]`` header
    before the fork prologue, so the sentinel is NOT at offset 0 of the first
    enqueue. A prefix-only (``startswith``) check classified every real fork as
    a non-fork. This mirrors the exact production shape produced by
    ``protocol.starting_prompt`` (see callstack/agent_callstack/protocol.py).
    """
    p = tmp_path / "parent.jsonl"
    _write(
        p,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
        ],
    )
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": (
                    "## Starting Task [a5ba828c]\n\n"
                    "You are running in a forked session — a child process "
                    "that inherited the full context of your parent agent.\n"
                    "Task: execute /task-a"
                ),
                "timestamp": "2026-04-24T10:00:00Z",
            },
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-fork-own", "timestamp": "2026-04-24T10:00:01Z", "type": "user"},
        ],
    )
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == {"fork"}


def test_unrelated_sessions_dont_classify_as_forks(tmp_path: Path):
    a = tmp_path / "x.jsonl"
    b = tmp_path / "y.jsonl"
    _write(a, [{"uuid": "uA", "timestamp": "2026-04-24T09:00:00Z", "type": "user"}])
    _write(b, [{"uuid": "uB", "timestamp": "2026-04-24T10:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    assert fd.fork_session_ids() == set()


def test_resume_in_callstack_family_is_not_a_fork(tmp_path: Path):
    """A ``--resume`` continuation shares the parent's head uuid but lacks the
    callstack fork-prologue. When at least one sibling carries the prologue
    (i.e. callstack runtime is in use), unmarked siblings must remain visible.
    """
    parent = tmp_path / "parent.jsonl"
    _write(
        parent,
        [
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
        ],
    )
    # User resumed parent — Claude Code rewrites the session into a new file
    # with the parent's head uuid, but the first queue-op carries the user's
    # own prompt, not the runtime fork-prologue.
    resume = tmp_path / "resume.jsonl"
    _write(
        resume,
        [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "Continue working on README updates",
                "timestamp": "2026-04-24T10:00:00Z",
            },
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-resume-own", "timestamp": "2026-04-24T10:00:01Z", "type": "user"},
        ],
    )
    # True callstack fork spawned from the resume — first queue-op content
    # begins with the runtime prologue.
    fork = tmp_path / "fork.jsonl"
    _write(
        fork,
        [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "You are running in a forked session — execute /task-a",
                "timestamp": "2026-04-24T11:00:00Z",
            },
            {"uuid": "u-head", "timestamp": "2026-04-24T09:00:00Z", "type": "user"},
            {"uuid": "u-fork-own", "timestamp": "2026-04-24T11:00:01Z", "type": "user"},
        ],
    )
    fd = ForkDetector(tmp_path)
    forks = fd.fork_session_ids()
    assert forks == {"fork"}, forks


def test_cache_is_invalidated_on_growth(tmp_path: Path):
    s = tmp_path / "g.jsonl"
    _write(s, [{"uuid": "u1", "timestamp": "2026-04-24T09:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    fd.fork_session_ids()  # warm cache
    time.sleep(0.01)
    # Append a marker-bearing fork sibling. The fork classification must
    # see the new file even though the original probe set was cached.
    f = tmp_path / "fork.jsonl"
    _write(
        f,
        [
            {
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "You are running in a forked session — execute /task-a",
                "timestamp": "2026-04-24T10:00:00Z",
            },
            {"uuid": "u1", "timestamp": "2026-04-24T10:00:00Z", "type": "user"},
        ],
    )
    forks = fd.fork_session_ids()
    assert forks == {"fork"}


def test_refresh_skipped_within_ttl_and_when_signature_unchanged(
    tmp_path: Path, monkeypatch
):
    """Back-to-back public calls must not re-glob the project dir.

    GET /sessions calls into the detector 3-4× per request; we want exactly
    one filesystem pass per request, not one per call.
    """
    s = tmp_path / "s.jsonl"
    _write(s, [{"uuid": "u1", "timestamp": "2026-04-24T09:00:00Z", "type": "user"}])
    fd = ForkDetector(tmp_path)
    fd.fork_session_ids()  # warm

    import os
    scan_calls = {"n": 0}
    original_scandir = os.scandir

    def counting_scandir(path):  # noqa: ANN001
        if Path(path) == tmp_path:
            scan_calls["n"] += 1
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", counting_scandir)

    # TTL fast-path: three back-to-back calls must do zero filesystem scans.
    fd.fork_session_ids()
    fd.is_fork("s")
    fd.children_of("s")
    assert scan_calls["n"] == 0, "TTL fast-path should suppress all scans"

    # Force past the detector's TTL AND drop the shared listing cache: the
    # signature-skip path still does exactly one scandir, but no probe rebuild.
    fd._last_refresh_ts = 0.0
    from unwind.projects import invalidate_jsonl_listing
    invalidate_jsonl_listing(tmp_path)
    fd.fork_session_ids()
    assert scan_calls["n"] == 1
