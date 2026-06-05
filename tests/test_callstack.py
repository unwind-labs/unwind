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


def _parent_with_child_report(
    dir_: Path,
    *,
    parent_status: str,
    child_status: str,
    started_at: str = "2026-01-01T00:00:00+00:00",
    invoke_id: str = "20260101T000000-root",
) -> None:
    """Write a report: MAIN → FORK-A(``parent_status``) → GRAND(``child_status``).

    FORK-A is the session under test in the wall-rule cases; GRAND is its
    descendant whose status would (without the wall) escalate upward.
    """
    _write_report(
        dir_,
        invoke_id,
        {
            "invoke_id": invoke_id,
            "parent_session": "MAIN",
            "started_at": started_at,
            "status": "complete",
            "tasks": [
                {
                    "task": "/fork-a",
                    "status": parent_status,
                    "depth": 1,
                    "session_id": "FORK-A",
                    "children": [
                        {
                            "task": "/grand",
                            "status": child_status,
                            "depth": 2,
                            "session_id": "GRAND",
                        }
                    ],
                }
            ],
        },
    )


def test_failed_parent_with_live_descendant_returns_failed(tmp_path: Path):
    """The bug this fix targets: a failed invocation whose child is still
    marked ``running`` in a stale report.yaml. The child's ``running`` is
    debt left behind when the parent crashed — not live work — so the
    terminal-ancestor wall pins FORK-A to ``failed`` instead of letting
    the stale descendant resurrect it to ``live`` (which kept the CALL row
    pulsing forever)."""
    log = tmp_path / "log"
    _parent_with_child_report(log, parent_status="error", child_status="running")
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "failed"


def test_done_parent_with_live_descendant_returns_done(tmp_path: Path):
    """Same wall, ``done`` arm: a completed invocation can't have a
    genuinely live descendant (the runtime gates return on children
    returning first), so a child still marked ``running`` is stale and
    must not pull FORK-A back to ``live``."""
    log = tmp_path / "log"
    _parent_with_child_report(log, parent_status="complete", child_status="running")
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "done"


def test_live_parent_with_done_descendant_returns_live(tmp_path: Path):
    """Regression for the original escalation purpose: a live parent with a
    finished child stays ``live``. The wall only fires on terminal OWN
    status, so a non-terminal parent still merges its subtree as before."""
    log = tmp_path / "log"
    _parent_with_child_report(log, parent_status="running", child_status="complete")
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "live"


def test_yield_parent_with_live_descendant_returns_live(tmp_path: Path):
    """``yield`` is deliberately NOT a wall: a yielded parent waiting on
    user input can legitimately sit above a still-running descendant, so
    the live child still escalates the yielded parent to ``live``. This is
    the case that distinguishes the wall from a blanket 'non-live is
    terminal' rule."""
    log = tmp_path / "log"
    _parent_with_child_report(log, parent_status="yielded", child_status="running")
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "live"


def test_resume_completed_returns_done_via_latest_view(tmp_path: Path):
    """The original 'resume completed' fix must still hold under the wall,
    and this test isolates the two mechanisms coexisting.

    An older report records FORK-A as ``yielded``; a newer report records
    FORK-A as ``complete`` but leaves its GRAND child frozen at ``running``
    (stale debt). ``_latest_view`` dedupes by session_id keeping the newer
    TaskNode, so FORK-A's own status is ``done``. WITHOUT the wall the
    ``running`` GRAND would merge to ``live`` and the CALL row would keep
    pulsing; the wall pins FORK-A to ``done`` once the resume lands. Both
    the dedup and the wall are required for the assertion to hold — flip
    either off and this returns ``live``."""
    log = tmp_path / "log"
    _parent_with_child_report(
        log,
        parent_status="yielded",
        child_status="running",
        started_at="2026-01-01T00:00:00+00:00",
        invoke_id="20260101T000000-orig",
    )
    _parent_with_child_report(
        log,
        parent_status="complete",
        child_status="running",
        started_at="2026-01-01T01:00:00+00:00",
        invoke_id="20260101T010000-resume",
    )
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "done"


def test_aggregate_status_wall_fires_for_orchestrator_with_stale_subcall(tmp_path: Path):
    """The actual phase4 bug: an ORCHESTRATOR session that itself spawned
    sub-``/call``s. ORCH is both a task (recorded ``failed`` by its caller
    ROOT) AND a ``parent_session`` of its own sub-reports — the latest of
    which is frozen at ``running`` because ORCH crashed before the sub-call
    returned.

    The wall must read ORCH's OWN verdict from ``canonical[ORCH]``
    (``failed``) ONLY. If it also folded in ``root_status[ORCH]`` (the
    ``running`` sub-call ORCH spawned), the own merge would resolve to
    ``live`` and the wall would never fire — returning ``live`` and leaving
    the CALL row pulsing forever. This is the regression the canonical-only
    fix closes; the same-report topology of the other wall tests can't
    reach it because there FORK-A is never a ``parent_session``."""
    log = tmp_path / "log"
    # Report 1: ROOT invoked ORCH; ORCH's task verdict is failed.
    _write_report(
        log,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "parent_session": "ROOT",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "tasks": [
                {"task": "/orch", "status": "error", "depth": 1, "session_id": "ORCH"}
            ],
        },
    )
    # Report 2: ORCH itself spawned a sub-/call, frozen at running.
    _write_report(
        log,
        "20260101T000500-sub",
        {
            "invoke_id": "20260101T000500-sub",
            "parent_session": "ORCH",
            "started_at": "2026-01-01T00:05:00+00:00",
            "status": "running",
            "tasks": [
                {"task": "/playbook", "status": "running", "depth": 1, "session_id": "PLAYBOOK"}
            ],
        },
    )
    ci = CallstackIndex(log)
    # root_status[ORCH] == "running" (its sub-call), but ORCH's own verdict
    # is failed — the wall must pin it.
    assert ci.aggregate_status_for_session("ORCH") == "failed"


def test_done_parent_with_yielded_child_returns_done(tmp_path: Path):
    """When the wall fires it suppresses descendants of ANY status, not just
    stale ``running``. A ``done`` parent with a ``yielded`` child returns
    ``done`` — even though ``merge(["done", "yield"])`` would otherwise be
    ``yield``. A returned parent pins the verdict regardless of what its
    descendants report; this pins that documented behavior."""
    log = tmp_path / "log"
    _parent_with_child_report(log, parent_status="complete", child_status="yielded")
    ci = CallstackIndex(log)
    assert ci.aggregate_status_for_session("FORK-A") == "done"


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


def test_extra_report_paths_read_from_out_of_tree_dir(tmp_path: Path):
    """Bug 2: when the runtime anchors its log dir to the ROOT invocation's
    cwd, a session viewed under a different project has an EMPTY primary log
    dir. The ``extra_report_paths`` provider recovers the out-of-tree
    report.yaml (path harvested from the session's tool_result envelope) so the
    call tree still resolves.
    """
    primary = tmp_path / "viewed" / ".claude" / "callstack" / "log"  # never created
    out_of_tree = tmp_path / "root_project" / ".claude" / "callstack" / "log"
    _write_report(
        out_of_tree,
        "20260101T000000-root",
        {
            "invoke_id": "20260101T000000-root",
            "parent_session": "ROOT",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "tasks": [
                {"task": "/a", "status": "complete", "depth": 1, "session_id": "A"}
            ],
        },
    )
    foreign = out_of_tree / "20260101T000000-root" / "report.yaml"

    # No primary log dir at all — everything must come from the provider.
    ci = CallstackIndex(primary, extra_report_paths=lambda: [foreign])
    assert ci.has_logs  # extras count, even with no primary dir
    reps = ci.all_reports()
    assert len(reps) == 1 and reps[0].parent_session == "ROOT"
    assert [t.session_id for t in reps[0].tasks] == ["A"]


def test_extra_report_paths_deduped_against_primary(tmp_path: Path):
    """A report reachable both via the primary log dir and the provider is
    parsed exactly once (deduped by resolved path)."""
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-r",
        {
            "invoke_id": "20260101T000000-r",
            "parent_session": "ROOT",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "tasks": [
                {"task": "/a", "status": "complete", "depth": 1, "session_id": "A"}
            ],
        },
    )
    same = log / "20260101T000000-r" / "report.yaml"
    ci = CallstackIndex(log, extra_report_paths=lambda: [same])
    assert len(ci.all_reports()) == 1


def test_provider_exception_degrades_to_primary(tmp_path: Path):
    """A failing provider must not break the primary log dir read."""
    log = tmp_path / "log"
    _write_report(
        log,
        "20260101T000000-r",
        {
            "invoke_id": "20260101T000000-r",
            "parent_session": "ROOT",
            "started_at": "2026-01-01T00:00:00+00:00",
            "status": "complete",
            "tasks": [{"task": "/a", "status": "complete", "depth": 1, "session_id": "A"}],
        },
    )

    def boom() -> list:
        raise RuntimeError("provider exploded")

    ci = CallstackIndex(log, extra_report_paths=boom)
    assert ci.has_logs
    assert len(ci.all_reports()) == 1
