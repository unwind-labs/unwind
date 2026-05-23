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


def test_build_subtree_accepts_new_call_kind(tmp_path: Path):
    """The runtime now emits `kind: "call"` / `kind: "call_resume"` in
    report.yaml (replacing legacy `invoke`/`invoke_parallel`/`invoke_resume`).
    Old reports stay readable via the legacy strings; new ones must work
    with the new vocabulary."""
    log = tmp_path / "log"
    _write_report(
        log,
        "20260201T000000-root",
        {
            "invoke_id": "20260201T000000-root",
            "kind": "call",
            "parent_session": "ROOT",
            "started_at": "2026-02-01T00:00:00+00:00",
            "ended_at": "2026-02-01T00:00:30+00:00",
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
                }
            ],
        },
    )
    _write_report(
        log,
        "20260201T000010-resume",
        {
            "invoke_id": "20260201T000010-resume",
            "kind": "call_resume",
            "parent_session": "ROOT",
            "started_at": "2026-02-01T00:00:10+00:00",
            "ended_at": "2026-02-01T00:00:15+00:00",
            "status": "complete",
            "tasks": [
                {
                    "id": "a2",
                    "task": "/task-a-resumed",
                    "status": "complete",
                    "depth": 1,
                    "session_id": "CHILD-A",
                    "duration_seconds": 1.5,
                    "summary": "resumed a",
                }
            ],
        },
    )
    ci = CallstackIndex(log)
    assert ci.has_logs
    subtree = ci.build_subtree("ROOT")
    # Both invocations target CHILD-A; CallstackIndex should surface them.
    sessions = {n.session_id for n in subtree}
    assert "CHILD-A" in sessions


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


def test_direct_invocations_of_returns_one_per_report(tmp_path: Path):
    """When the same parent → child edge appears in N separate reports
    (e.g. the parent invoked the same child three times via callstack
    Skill, producing three report.yaml files), ``direct_invocations_of``
    must return N TaskNodes, sorted by ``started_at``. ``direct_children_of``
    deduplicates the same edge to one entry — this method does not.
    """
    log = tmp_path / "log"
    for i, ts in enumerate(
        ["2026-05-03T19:55:42+00:00", "2026-05-03T21:09:00+00:00", "2026-05-03T21:09:40+00:00"]
    ):
        _write_report(
            log,
            f"20260503T19554{i}-r{i}",
            {
                "invoke_id": f"20260503T19554{i}-r{i}",
                "parent_session": "MAIN",
                "started_at": ts,
                "ended_at": ts,
                "status": "complete",
                "tasks": [
                    {
                        "task": "/verify-mfa",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "MFA-SESSION",
                        "children": [
                            {
                                "task": "/check-code-expiry",
                                "status": "complete",
                                "depth": 2,
                                "session_id": "EXPIRY-SESSION",
                            }
                        ],
                    }
                ],
            },
        )
    ci = CallstackIndex(log)

    # MAIN's direct invocations: three TaskNodes for MFA-SESSION, one
    # per report, in chronological order.
    mfa_invs = ci.direct_invocations_of("MAIN")
    assert [n.session_id for n in mfa_invs] == ["MFA-SESSION", "MFA-SESSION", "MFA-SESSION"]
    assert [n.invoke_id for n in mfa_invs] == [
        "20260503T195540-r0",
        "20260503T195541-r1",
        "20260503T195542-r2",
    ]

    # MFA-SESSION's direct invocations: three TaskNodes for EXPIRY-SESSION,
    # one nested inside each report.
    expiry_invs = ci.direct_invocations_of("MFA-SESSION")
    assert [n.session_id for n in expiry_invs] == [
        "EXPIRY-SESSION",
        "EXPIRY-SESSION",
        "EXPIRY-SESSION",
    ]
    # Each carries the invoke_id of its owning report.
    assert {n.invoke_id for n in expiry_invs} == {
        "20260503T195540-r0",
        "20260503T195541-r1",
        "20260503T195542-r2",
    }

    # Compared to direct_children_of, which deduplicates by session_id.
    assert [n.session_id for n in ci.direct_children_of("MFA-SESSION")] == ["EXPIRY-SESSION"]


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
    # Canonical "done" — the boundary translator in unwind.status maps the
    # callstack ``complete``/``failed``/``error`` family to canonical
    # ``done``/``failed``. Callers (sessions_api, cli_cmds.session) must
    # therefore override with live-process detection for "main" sessions
    # whose terminal-callstack-status would otherwise mark them done.
    assert ci.aggregate_status_for_session("MAIN") == "done"
    assert ci.is_callstack_task("MAIN") is False


def test_latest_view_is_memoized_until_files_change(tmp_path: Path, monkeypatch):
    """A /sessions response calls aggregate_status + is_callstack_task per row.
    Each goes through _latest_view, which used to do a full O(reports × tree)
    rebuild on every call. After T9 it's memoized by _log_signature, so the
    expensive recursion runs once per fingerprint, not per row.
    """
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-r",
        {
            "invoke_id": "20260101T000000-r",
            "parent_session": "ROOT",
            "status": "complete",
            "tasks": [
                {"task": "/a", "status": "complete", "depth": 1, "session_id": "A"}
            ],
        },
    )
    ci = CallstackIndex(log)

    # Prime the cache.
    ci._latest_view()
    initial_id = id(ci._view_cached)
    assert initial_id is not None

    # Simulate many rows hitting the view; identity must not change.
    for _ in range(50):
        ci._latest_view()
        ci.reports_by_parent()
    assert id(ci._view_cached) == initial_id

    # Touch the file: signature must invalidate and rebuild.
    import os, time
    report_path = log / "20260101T000000-r" / "report.yaml"
    st = report_path.stat()
    os.utime(report_path, (st.st_atime, st.st_mtime + 1))
    # Force size to differ as well for belt-and-braces signature change.
    ci._latest_view()
    assert id(ci._view_cached) != initial_id
