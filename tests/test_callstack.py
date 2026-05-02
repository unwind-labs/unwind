from pathlib import Path

import yaml

from unwind.callstack import CallstackIndex


def _write_report(dir_: Path, invoke_id: str, payload: dict) -> None:
    d = dir_ / invoke_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "report.yaml").write_text(yaml.safe_dump(payload))


def test_build_subtree_nests_children_recursively(tmp_path: Path):
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "kind": "invoke_parallel",
            "parent_session": "ROOT",
            "started_at": "2026-01-01T00:00:00+00:00",
            "ended_at": "2026-01-01T00:00:30+00:00",
            "status": "complete",
            "tasks": [
                {
                    "id": "a",
                    "task": "/task-a",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "CHILD-A",
                    "duration_seconds": 5.0,
                    "summary": "did a",
                    "children": [
                        {
                            "id": "b",
                            "task": "/task-b",
                            "status": "complete",
                            "depth": 2,
                            "session_id": "GRAND-B",
                            "duration_seconds": 1.5,
                            "summary": "did b",
                        }
                    ],
                }
            ],
        },
    )
    ci = CallstackIndex(log)
    assert ci.has_logs
    subtree = ci.build_subtree("ROOT")
    assert len(subtree) == 1
    child = subtree[0]
    assert child.session_id == "CHILD-A"
    assert child.summary == "did a"
    assert len(child.children) == 1
    grand = child.children[0]
    assert grand.session_id == "GRAND-B"
    assert grand.task == "/task-b"


def test_independent_later_invocations_are_merged(tmp_path: Path):
    """CHILD-A spawned its own invocation later; should attach under CHILD-A."""
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "parent_session": "ROOT",
            "tasks": [
                {
                    "task": "/task-a",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "CHILD-A",
                    "summary": "first pass",
                }
            ],
        },
    )
    _write_report(
        log,
        "20260101T000100-later",
        {
            "invoke_id": "20260101T000100-later",
            "parent_session": "CHILD-A",
            "tasks": [
                {
                    "task": "/followup",
                    "status": "running",
                    "depth": 1,
                    "session_id": "GRAND-B",
                    "summary": "later work",
                }
            ],
        },
    )
    ci = CallstackIndex(log)
    subtree = ci.build_subtree("ROOT")
    child = subtree[0]
    assert child.session_id == "CHILD-A"
    grand = child.children[0]
    assert grand.session_id == "GRAND-B"
    assert grand.task == "/followup"
    assert grand.status == "running"


def test_missing_log_dir(tmp_path: Path):
    ci = CallstackIndex(tmp_path / "does-not-exist")
    assert not ci.has_logs
    assert ci.build_subtree("ANYTHING") == []


def test_is_callstack_task_distinguishes_main_session_from_forks(tmp_path: Path):
    """The user's main session is a ``parent_session`` in callstack reports
    but never a ``TaskNode``. Forks are the opposite. ``is_callstack_task``
    must say False for the main session and True for any fork — the sessions
    API uses this to decide whether to trust callstack's terminal status or
    fall through to live process detection.
    """
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "parent_session": "MAIN",
            "status": "complete",
            "tasks": [
                {
                    "task": "/task-a",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "FORK-A",
                }
            ],
        },
    )
    ci = CallstackIndex(log)
    assert ci.is_callstack_task("FORK-A") is True
    # MAIN never appears as a task — it's only a parent_session.
    assert ci.is_callstack_task("MAIN") is False
    # Unknown sessions also return False.
    assert ci.is_callstack_task("UNRELATED") is False


def test_aggregate_status_returns_terminal_for_main_when_chain_complete(tmp_path: Path):
    """Sanity check that ``aggregate_status_for_session`` DOES return
    ``complete`` for a main session whose entire fork chain is done — this
    is the situation the sessions-api caller has to override (so a still-
    running main session shows live, not done)."""
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "parent_session": "MAIN",
            "status": "complete",
            "tasks": [
                {
                    "task": "/task-a",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "FORK-A",
                }
            ],
        },
    )
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("MAIN") == "complete"
    assert ci.is_callstack_task("MAIN") is False
