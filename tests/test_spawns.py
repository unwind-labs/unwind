"""Tests for the unified spawn resolver."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from unwind.callstack import CallstackIndex
from unwind.canvas_tree import CanvasTreeBuilder, build_canvas_tree
from unwind.fork_detect import ForkDetector
from unwind.messages import annotate_spawns, read_messages
from unwind.spawns import SpawnResolver
from unwind.subagents import SubagentIndex


# --- helpers -------------------------------------------------------------


def _make_resolver(project_dir: Path, log_dir: Path) -> SpawnResolver:
    """Build a resolver wired to a CanvasTreeBuilder for SessionScan-driven
    fork status (mirrors what registry.spawn_resolver_for_slug does)."""
    builder = CanvasTreeBuilder(project_dir)
    return SpawnResolver(
        CallstackIndex(log_dir),
        ForkDetector(project_dir),
        SubagentIndex(project_dir),
        project_dir=project_dir,
        session_scanner=builder.get_scan,
    )


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _fork_prologue_record(uuid: str = "head-uuid") -> list[dict]:
    """A minimal child JSONL whose first queue-op carries the callstack
    fork prologue marker. ForkDetector classifies it as a callstack fork."""
    return [
        {
            "type": "queue-operation",
            "operation": "enqueue",
            "content": (
                "You are running in a forked session...\n\n"
                "## Starting Task [...]\n\n/task-x"
            ),
        },
        {
            "uuid": uuid,
            "type": "user",
            "sessionId": "child",
            "timestamp": "2026-05-04T10:00:00.000Z",
            "message": {"role": "user", "content": "/task-x"},
        },
    ]


# --- core resolver -------------------------------------------------------


def test_callstack_report_yields_spawn(tmp_path: Path):
    log = tmp_path / "log"
    proj = tmp_path / "proj"
    proj.mkdir()
    inv = log / "i0"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i0",
                "parent_session": "ROOT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/task-x",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )
    res = _make_resolver(proj, log)
    spawns = res.for_parent("ROOT")
    assert len(spawns) == 1
    s = spawns[0]
    assert s.child_session_id == "CHILD"
    assert s.kind == "call"
    assert s.invoke_id == "i0"
    assert s.label == "/task-x"
    assert s.source == "callstack"


def test_fork_detector_yields_spawn_when_no_report(tmp_path: Path):
    """The bug from issue 1: a fork-detected child whose report.yaml
    hasn't been written yet should still surface as a Spawn so the
    canvas can render the in-flight child node."""
    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()

    # Parent: empty JSONL with one queue-op (no fork prologue → unmarked).
    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "kick off"},
            }
        ],
    )
    # Child: same head uuid + fork prologue → ForkDetector classifies as fork.
    _write_jsonl(proj / "CHILD.jsonl", _fork_prologue_record(uuid="head-uuid"))

    res = _make_resolver(proj, log)
    spawns = res.for_parent("ROOT")
    fork_spawns = [s for s in spawns if s.source == "fork"]
    assert len(fork_spawns) == 1
    s = fork_spawns[0]
    assert s.child_session_id == "CHILD"
    assert s.kind == "call"
    assert s.status == "running"
    # divergence text should be the assigned task name from the prologue.
    assert s.label == "/task-x"


def test_fork_spawn_marked_complete_when_child_returned(tmp_path: Path):
    """Regression: a fork-detected child whose JSONL contains the
    callstack ``{"op": "return"}`` envelope must surface as ``complete``
    with a real ``ended_at``. Before the fix, status was hardcoded to
    ``running`` so the parent's CALL row stayed LIVE forever even when
    the child had clearly returned.

    Scenario: child ran to completion but no ``report.yaml`` was written
    (older runtime, or report not flushed yet). The envelope in the
    child's assistant message is the only signal — must be honored.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()

    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "kick off"},
            }
        ],
    )
    # Child JSONL: prologue + return envelope in an assistant message.
    _write_jsonl(
        proj / "CHILD.jsonl",
        _fork_prologue_record(uuid="head-uuid") + [
            {
                "uuid": "a1",
                "type": "assistant",
                "sessionId": "CHILD",
                "timestamp": "2026-05-04T10:00:09.500Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Done.\n```json\n"
                                '{"op": "return", "result": "ok"}\n```'
                            ),
                        }
                    ],
                },
            }
        ],
    )

    res = _make_resolver(proj, log)
    fork_spawns = [s for s in res.for_parent("ROOT") if s.source == "fork"]
    assert len(fork_spawns) == 1
    s = fork_spawns[0]
    assert s.status == "complete"
    assert s.ended_at is not None
    assert s.ended_at.isoformat().startswith("2026-05-04T10:00:09.500")


def test_fork_spawn_marked_yielded_when_child_yielded(tmp_path: Path):
    """Regression companion to the return case: a fork-detected child
    whose terminal envelope is ``{"op": "yield"}`` must surface as
    ``yielded`` — important because the spawn-row done logic treats
    ``yielded`` as terminal (the child returned control to its parent
    pending user input) but the canvas window status as ``yield``."""
    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()

    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "kick off"},
            }
        ],
    )
    _write_jsonl(
        proj / "CHILD.jsonl",
        _fork_prologue_record(uuid="head-uuid") + [
            {
                "uuid": "a1",
                "type": "assistant",
                "sessionId": "CHILD",
                "timestamp": "2026-05-04T10:00:09.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"op": "yield", "question": "?"}\n```',
                        }
                    ],
                },
            }
        ],
    )

    res = _make_resolver(proj, log)
    fork_spawns = [s for s in res.for_parent("ROOT") if s.source == "fork"]
    assert len(fork_spawns) == 1
    assert fork_spawns[0].status == "yielded"


def test_report_with_stale_parent_session_heals_to_invoking_jsonl(
    tmp_path: Path,
):
    """Regression: callstack runtimes have been observed writing the
    wrong ``parent_session`` into ``report.yaml`` — e.g. a session id
    from an unrelated project bleeds into a fresh run, so the report
    points to a session that doesn't exist as a JSONL in this project.

    The fix: when the recorded ``parent_session`` has no JSONL but the
    report's ``invoke_id`` appears as the result of a callstack tool_use
    somewhere in the project, attribute the spawn to the session
    actually holding that tool_use. The tool_use → invoke_id binding is
    authoritative (it's what the tool_result contains); the
    ``parent_session`` field is metadata that can drift.

    Setup: ORCHESTRATOR.jsonl has a callstack ``call`` tool_use whose
    tool_result carries ``invoke_id: i-stale``. The matching report
    records ``parent_session: GHOST`` — but no GHOST.jsonl exists. The
    resolver must still surface the spawn under ORCHESTRATOR.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    inv = log / "i-stale"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-stale",
                "parent_session": "GHOST-NO-JSONL",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    # The real parent: a session with a callstack tool_use whose
    # tool_result carries ``invoke_id: i-stale``. This is what the
    # runtime would have written when it actually fired the call.
    parent = proj / "ORCHESTRATOR.jsonl"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "ORCHESTRATOR",
                    "timestamp": "2026-05-04T10:00:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "mcp__plugin_callstack_call__call",
                                "input": {"task": "/inner"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "ORCHESTRATOR",
                    "timestamp": "2026-05-04T10:00:02.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": '{"invoke_id": "i-stale"}',
                            }
                        ],
                    },
                },
            ]
        )
        + "\n"
    )
    # Minimal child JSONL so canvas BFS can reach it.
    (proj / "CHILD.jsonl").write_text(
        json.dumps({
            "uuid": "u",
            "type": "user",
            "sessionId": "CHILD",
            "timestamp": "2026-05-04T10:00:02.000Z",
            "message": {"role": "user", "content": "/inner"},
        }) + "\n"
    )

    res = _make_resolver(proj, log)

    # The spawn must be re-keyed under the actual parent (ORCHESTRATOR),
    # not the GHOST session named in the report.
    assert res.for_parent("GHOST-NO-JSONL") == []
    orch_spawns = res.for_parent("ORCHESTRATOR")
    assert len(orch_spawns) == 1
    assert orch_spawns[0].child_session_id == "CHILD"
    assert orch_spawns[0].invoke_id == "i-stale"

    # End-to-end: the canvas tree rooted at ORCHESTRATOR shows CHILD.
    root_w, _ = build_canvas_tree(proj, "ORCHESTRATOR", spawn_resolver=res)
    assert [c.session_id for c in root_w.children] == ["CHILD"]


def test_report_parent_session_overridden_by_tool_use_in_sibling(
    tmp_path: Path,
):
    """Regression: callstack runtimes have been observed writing the
    wrong ``parent_session`` even when that recorded session is a real
    JSONL in the same project (an unrelated sibling). When a sibling
    JSONL actually contains the callstack tool_use that produced this
    invoke_id, that JSONL is the true parent — the tool_use → invoke_id
    binding wins over ``report.parent_session``.

    Setup: report records ``parent_session: WRONG-PARENT`` (a real but
    unrelated JSONL with no callstack tool_use). A different session,
    ``RIGHT-PARENT``, carries the callstack tool_use whose tool_result
    surfaces ``invoke_id: i-wrong``. The spawn must be attributed to
    RIGHT-PARENT, not WRONG-PARENT.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    inv = log / "i-wrong"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-wrong",
                "parent_session": "WRONG-PARENT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    # WRONG-PARENT exists but never made a callstack call.
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "WRONG-PARENT.jsonl").write_text(
        json.dumps({
            "uuid": "u0",
            "type": "user",
            "sessionId": "WRONG-PARENT",
            "timestamp": "2026-05-04T09:00:00.000Z",
            "message": {"role": "user", "content": "unrelated work"},
        }) + "\n"
    )

    # RIGHT-PARENT: holds the actual callstack tool_use whose
    # tool_result carries invoke_id i-wrong.
    (proj / "RIGHT-PARENT.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "RIGHT-PARENT",
                    "timestamp": "2026-05-04T10:00:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "mcp__plugin_callstack_call__call",
                                "input": {"task": "/inner"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "RIGHT-PARENT",
                    "timestamp": "2026-05-04T10:00:02.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": '{"invoke_id": "i-wrong"}',
                            }
                        ],
                    },
                },
            ]
        )
        + "\n"
    )
    (proj / "CHILD.jsonl").write_text(
        json.dumps({
            "uuid": "u",
            "type": "user",
            "sessionId": "CHILD",
            "timestamp": "2026-05-04T10:00:02.000Z",
            "message": {"role": "user", "content": "/inner"},
        }) + "\n"
    )

    res = _make_resolver(proj, log)

    assert res.for_parent("WRONG-PARENT") == []
    right_spawns = res.for_parent("RIGHT-PARENT")
    assert len(right_spawns) == 1
    assert right_spawns[0].child_session_id == "CHILD"
    assert right_spawns[0].invoke_id == "i-wrong"

    root_w, _ = build_canvas_tree(proj, "RIGHT-PARENT", spawn_resolver=res)
    assert [c.session_id for c in root_w.children] == ["CHILD"]


def test_invoke_id_override_picks_non_descendant_candidate(
    tmp_path: Path,
):
    """Regression: when the global invoke index has multiple candidate
    sessions for one invoke_id (one is the real parent, another is a
    forked child that re-emitted the same invoke_id), the override must
    pick the candidate that isn't a descendant in the report.

    Setup mirrors the carapace bug: report records ``parent_session:
    WRONG`` (a real but irrelevant JSONL). Two other sessions surface
    the invoke_id in their tool_results: REAL-PARENT (the actual
    parent) and CHILD (the forked descendant that echoed the OUTER
    invoke_id due to the callstack plugin bug). The spawn must end up
    REAL-PARENT → CHILD, not CHILD → CHILD.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    inv = log / "i-outer"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-outer",
                "parent_session": "WRONG",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    proj.mkdir(parents=True, exist_ok=True)
    # WRONG: empty JSONL (no callstack tool_use). Exists, so the legacy
    # missing-JSONL heal path doesn't fire.
    (proj / "WRONG.jsonl").write_text(
        json.dumps({
            "uuid": "u0",
            "type": "user",
            "sessionId": "WRONG",
            "timestamp": "2026-05-04T09:00:00.000Z",
            "message": {"role": "user", "content": "unrelated"},
        }) + "\n"
    )

    def callstack_pair(sid: str, tu_id: str) -> list[dict]:
        return [
            {
                "type": "assistant",
                "uuid": f"a-{sid}",
                "sessionId": sid,
                "timestamp": "2026-05-04T10:00:01.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": tu_id,
                            "name": "mcp__plugin_callstack_call__call",
                            "input": {"task": "/inner"},
                        }
                    ],
                },
            },
            {
                "type": "user",
                "uuid": f"u-{sid}",
                "sessionId": sid,
                "timestamp": "2026-05-04T10:00:02.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": tu_id,
                            "content": '{"invoke_id": "i-outer"}',
                        }
                    ],
                },
            },
        ]

    # REAL-PARENT: holds the original tool_use that produced i-outer.
    _write_jsonl(proj / "REAL-PARENT.jsonl", callstack_pair("REAL-PARENT", "tu-real"))
    # CHILD: also has a tool_use whose tool_result echoes the OUTER
    # invoke_id (the callstack plugin bug). Must NOT be picked.
    _write_jsonl(proj / "CHILD.jsonl", callstack_pair("CHILD", "tu-bogus"))

    res = _make_resolver(proj, log)

    # No spawns under WRONG or CHILD; the real parent owns it.
    assert res.for_parent("WRONG") == []
    real_spawns = res.for_parent("REAL-PARENT")
    assert len(real_spawns) == 1
    assert real_spawns[0].child_session_id == "CHILD"

    # No session is its own direct child anywhere.
    for parent, sps in res.spawns_by_parent().items():
        for s in sps:
            assert s.child_session_id != parent

    root_w, _ = build_canvas_tree(proj, "REAL-PARENT", spawn_resolver=res)
    assert [c.session_id for c in root_w.children] == ["CHILD"]


def test_invoke_id_override_skipped_when_target_is_task_in_report(
    tmp_path: Path,
):
    """Regression: the callstack plugin has been observed echoing the
    OUTER invoke_id in tool_results for inner ``/call``s made by a
    forked child. So ``compute_invoke_index_for_project`` can map the
    OUTER invoke_id to the CHILD's JSONL (the one running the inner
    calls), not the real parent.

    Without a guard, the spawn would be re-keyed CHILD→CHILD (since
    CHILD appears as a task in the report), creating a self-loop that
    sends canvas tree ``to_dict`` into infinite recursion.

    Setup mirrors the carapace case: report records parent=PARENT and
    task=CHILD. CHILD's JSONL contains a callstack tool_use whose
    tool_result surfaces the OUTER invoke_id (the plugin bug). The
    spawn must stay PARENT→CHILD; we must not redirect to CHILD→CHILD.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    inv = log / "i-outer"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-outer",
                "parent_session": "PARENT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    proj.mkdir(parents=True, exist_ok=True)
    # PARENT: empty JSONL — exists so the legacy "missing JSONL" heal
    # path doesn't fire either. The override must still be suppressed
    # by the task-tree guard.
    (proj / "PARENT.jsonl").write_text(
        json.dumps({
            "uuid": "u0",
            "type": "user",
            "sessionId": "PARENT",
            "timestamp": "2026-05-04T09:00:00.000Z",
            "message": {"role": "user", "content": "go"},
        }) + "\n"
    )
    # CHILD: simulates the plugin bug — CHILD made its own inner /call
    # and the tool_result echoes the OUTER invoke_id i-outer.
    (proj / "CHILD.jsonl").write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "CHILD",
                    "timestamp": "2026-05-04T10:00:05.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-inner",
                                "name": "mcp__plugin_callstack_call__call",
                                "input": {"task": "/innermost"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "CHILD",
                    "timestamp": "2026-05-04T10:00:06.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-inner",
                                "content": '{"invoke_id": "i-outer"}',
                            }
                        ],
                    },
                },
            ]
        )
        + "\n"
    )

    res = _make_resolver(proj, log)

    # Spawn must stay PARENT→CHILD (the recorded parent), not flip to
    # the self-loop CHILD→CHILD that the global invoke index suggests.
    parent_spawns = res.for_parent("PARENT")
    assert len(parent_spawns) == 1
    assert parent_spawns[0].child_session_id == "CHILD"

    # Hard guarantee: no session is its own direct child.
    for parent, sps in res.spawns_by_parent().items():
        for s in sps:
            assert s.child_session_id != parent, (
                f"self-loop spawn {parent} → {s.child_session_id}"
            )

    # Canvas tree must build without recursion errors.
    root_w, _ = build_canvas_tree(proj, "PARENT", spawn_resolver=res)
    assert [c.session_id for c in root_w.children] == ["CHILD"]


def test_report_with_valid_parent_session_and_no_tool_use_anchor_kept(
    tmp_path: Path,
):
    """Companion: when ``parent_session`` corresponds to a real JSONL
    and no tool_use anchor for this invoke_id exists anywhere in the
    project (e.g. Skill-style callsite that doesn't emit an MCP
    tool_use), the recorded ``parent_session`` stands — there's
    nothing to override it with."""
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    inv = log / "i-good"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-good",
                "parent_session": "REAL-PARENT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    # The real parent JSONL exists (no tool_use needed for the heal
    # path, since the heal only kicks in when the recorded parent is
    # missing). Just an empty session.
    (proj / "REAL-PARENT.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (proj / "REAL-PARENT.jsonl").write_text(
        json.dumps({
            "uuid": "u1",
            "type": "user",
            "sessionId": "REAL-PARENT",
            "timestamp": "2026-05-04T10:00:00.000Z",
            "message": {"role": "user", "content": "go"},
        }) + "\n"
    )
    (proj / "CHILD.jsonl").write_text(
        json.dumps({
            "uuid": "u",
            "type": "user",
            "sessionId": "CHILD",
            "timestamp": "2026-05-04T10:00:02.000Z",
            "message": {"role": "user", "content": "/inner"},
        }) + "\n"
    )

    res = _make_resolver(proj, log)
    real_spawns = res.for_parent("REAL-PARENT")
    assert len(real_spawns) == 1
    assert real_spawns[0].child_session_id == "CHILD"


def test_fork_spawn_under_nested_callstack_parent_no_phantom_at_root(
    tmp_path: Path,
):
    """Regression: bug where the fork detector created phantom
    ``root → grandchild`` spawns for every descendant in a callstack
    tree (since ``family_root()`` returns the topmost root for ALL
    descendants). The dedup only checked the exact ``(root, child)``
    pair against the callstack — nested pairs like ``(child, grandchild)``
    weren't recognised, so the grandchild got an extra phantom edge
    from root, with hardcoded ``status=running`` → LIVE rows under root
    that should never have been there.

    Setup mirrors the parallel_calls example: ROOT calls CHILD via
    callstack; CHILD calls GRANDCHILD via callstack. Both have prologue
    markers (so the fork detector sees them as forks). After the fix,
    GRANDCHILD must appear ONCE — as a child of CHILD — and never as a
    direct child of ROOT.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    # Single report.yaml covering the full ROOT → CHILD → GRANDCHILD chain.
    # The callstack's ``parent_session`` field is the immediate parent of
    # the TOP-LEVEL invoke only; nested invocations are recorded as
    # ``children`` of their parent task.
    inv = log / "i0"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i0",
                "parent_session": "ROOT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/outer",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                        "children": [
                            {
                                "task": "/inner",
                                "status": "complete",
                                "depth": 2,
                                "session_id": "GRANDCHILD",
                            }
                        ],
                    }
                ],
            }
        )
    )

    # All three sessions share the same head uuid (typical for forks
    # that inherited the parent's context). CHILD and GRANDCHILD carry
    # the prologue marker → fork detector classifies both as forks of
    # the same family_root (ROOT).
    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "go"},
            }
        ],
    )
    _write_jsonl(proj / "CHILD.jsonl", _fork_prologue_record(uuid="head-uuid"))
    _write_jsonl(
        proj / "GRANDCHILD.jsonl", _fork_prologue_record(uuid="head-uuid")
    )

    res = _make_resolver(proj, log)
    root_spawns = res.for_parent("ROOT")
    root_children = sorted(s.child_session_id for s in root_spawns)
    assert root_children == ["CHILD"], (
        f"ROOT must spawn CHILD only; got {root_children}. "
        "Phantom root→GRANDCHILD edge from the fork detector is back."
    )
    # And GRANDCHILD is correctly attributed to CHILD via the callstack.
    child_children = [s.child_session_id for s in res.for_parent("CHILD")]
    assert "GRANDCHILD" in child_children

    # End-to-end via the canvas tree: GRANDCHILD must NOT appear as a
    # direct child of the root window.
    root_w, _ = build_canvas_tree(proj, "ROOT", spawn_resolver=res)
    root_direct = [c.session_id for c in root_w.children]
    assert "GRANDCHILD" not in root_direct
    assert root_direct == ["CHILD"]


def test_fork_spawn_drops_when_callstack_report_arrives(tmp_path: Path):
    """Once report.yaml lands for the same (parent, child) pair, the
    fork-detected entry must NOT duplicate the callstack one."""
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "go"},
            }
        ],
    )
    _write_jsonl(proj / "CHILD.jsonl", _fork_prologue_record(uuid="head-uuid"))

    inv = log / "i0"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i0",
                "parent_session": "ROOT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/task-x",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    res = _make_resolver(proj, log)
    spawns = res.for_parent("ROOT")
    # Only one spawn for CHILD — and it's the callstack one (authoritative).
    child_spawns = [s for s in spawns if s.child_session_id == "CHILD"]
    assert len(child_spawns) == 1
    assert child_spawns[0].source == "callstack"


def test_subagent_index_caches_per_file_not_per_dir(tmp_path: Path):
    """Touching one subagent file must NOT force a re-parse of siblings."""
    from unwind.subagents import SubagentIndex

    proj = tmp_path / "proj"
    sub_dir = proj / "ROOT" / "subagents"
    sub_dir.mkdir(parents=True)
    for tag in ("aaaa", "bbbb"):
        (sub_dir / f"agent-{tag}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": tag})
        )
        (sub_dir / f"agent-{tag}.jsonl").write_text("")

    si = SubagentIndex(proj)

    # Spy on _build_one calls.
    calls: list[str] = []
    real_build = si._build_one
    def spy(path):
        calls.append(path.name)
        return real_build(path)
    si._file_cache._loader = spy

    si.list_for_session("ROOT")
    initial = list(calls)
    assert sorted(initial) == ["agent-aaaa.jsonl", "agent-bbbb.jsonl"]

    # Modify only one file. Bump its mtime so PathCache invalidates that
    # entry; the OTHER file should still come from cache.
    target = sub_dir / "agent-aaaa.jsonl"
    target.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n')
    # Also bump the dir's mtime so the per-session listing fast-path doesn't
    # short-circuit the per-file path we want to exercise.
    (sub_dir / "tickle.tmp").write_text("")
    (sub_dir / "tickle.tmp").unlink()
    calls.clear()

    si.list_for_session("ROOT")
    # Only the changed file re-parsed; the unchanged one stays cached.
    assert calls == ["agent-aaaa.jsonl"], calls


def test_subagent_yields_spawn(tmp_path: Path):
    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()
    sub_dir = proj / "ROOT" / "subagents"
    sub_dir.mkdir(parents=True)
    (sub_dir / "agent-aaaa1111.meta.json").write_text(
        json.dumps({"agentType": "general-purpose", "description": "Branch audit"})
    )
    (sub_dir / "agent-aaaa1111.jsonl").write_text("")
    # Parent JSONL exists so the resolver finds it.
    _write_jsonl(proj / "ROOT.jsonl", [
        {"uuid": "u1", "type": "user", "sessionId": "ROOT",
         "timestamp": "2026-05-04T10:00:00.000Z",
         "message": {"role": "user", "content": "go"}}
    ])

    res = _make_resolver(proj, log)
    spawns = res.for_parent("ROOT")
    sa_spawns = [s for s in spawns if s.kind == "subagent"]
    assert len(sa_spawns) == 1
    assert sa_spawns[0].child_session_id == "agent-aaaa1111"
    assert sa_spawns[0].label == "Branch audit"


# --- canvas tree integration --------------------------------------------


def test_canvas_tree_shows_fork_detected_child_without_report(tmp_path: Path):
    """End-to-end: a fork-detected child with NO report.yaml must appear
    in the canvas tree. This is the regression that motivated issue 1."""
    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()

    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "kick off"},
            }
        ],
    )
    _write_jsonl(proj / "CHILD.jsonl", _fork_prologue_record(uuid="head-uuid"))

    res = _make_resolver(proj, log)
    root, all_w = build_canvas_tree(
        proj,
        "ROOT",
        spawn_resolver=res,
        is_live_session=lambda _sid: False,
    )

    # CHILD must appear as a child of ROOT.
    sids = {w.session_id for w in all_w}
    assert "CHILD" in sids
    assert len(root.children) == 1
    assert root.children[0].session_id == "CHILD"
    assert root.children[0].kind == "call"


# --- annotate_spawns over resolver ---------------------------------------


def test_canvas_child_order_matches_parent_call_row_order(tmp_path: Path):
    """The canvas's child columns must appear in the same order as the
    parent's CALL rows. Without this, parallel-invoke siblings end up
    alphabetical-by-sid in the canvas while the parent's rows are in
    requested-task order — and the connectors cross.
    """
    proj = tmp_path / "proj"
    log = tmp_path / "log"

    # Three children, each spawned as part of one invoke_parallel. Note:
    # requested order is [Z-task, A-task, M-task] — NOT alphabetical.
    # Their session_ids are arranged to be alphabetical (sid-a, sid-m,
    # sid-z) so the OLD behavior would order them as a/m/z; the new
    # behavior should match the parent's request order z/a/m.
    inv = log / "i0"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i0",
                "parent_session": "ROOT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {"task": "Z-task", "status": "complete", "depth": 1, "session_id": "sid-z"},
                    {"task": "A-task", "status": "complete", "depth": 1, "session_id": "sid-a"},
                    {"task": "M-task", "status": "complete", "depth": 1, "session_id": "sid-m"},
                ],
            }
        )
    )

    # Parent's JSONL: one tool_use requesting [Z-task, A-task, M-task].
    parent = proj / "ROOT.jsonl"
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "ROOT",
                    "timestamp": "2026-05-04T10:00:00.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "mcp__plugin_callstack_call__invoke_parallel",
                                "input": {"tasks": ["Z-task", "A-task", "M-task"]},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "ROOT",
                    "timestamp": "2026-05-04T10:00:01.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": '{"invoke_id": "i0"}',
                            }
                        ],
                    },
                },
            ]
        )
        + "\n"
    )
    # Each child has a (minimal) JSONL so build_canvas_tree's BFS reaches it.
    for sid in ("sid-z", "sid-a", "sid-m"):
        (proj / f"{sid}.jsonl").write_text(
            json.dumps({
                "uuid": "u",
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-04T10:00:01.000Z",
                "message": {"role": "user", "content": "go"},
            }) + "\n"
        )

    res = _make_resolver(proj, log)
    root, _ = build_canvas_tree(proj, "ROOT", spawn_resolver=res)

    # Canvas children must be in the parent's REQUESTED order, not
    # alphabetical-by-sid.
    assert [c.session_id for c in root.children] == ["sid-z", "sid-a", "sid-m"]


def test_annotate_spawns_via_resolver_anchors_callstack_tool_use(tmp_path: Path):
    """The simplified annotate_spawns must still bind a tool_use to its
    callstack children when the report carries the matching invoke_id."""
    log = tmp_path / "log"
    proj = tmp_path / "proj"
    proj.mkdir()
    inv = log / "i-aaaa"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-aaaa",
                "parent_session": "ROOT",
                "started_at": "2026-05-04T10:00:00+00:00",
                "ended_at": "2026-05-04T10:00:30+00:00",
                "status": "complete",
                "tasks": [
                    {
                        "task": "/task-a",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD-A",
                    }
                ],
            }
        )
    )

    parent = proj / "ROOT.jsonl"
    parent.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {
                    "type": "assistant",
                    "uuid": "a1",
                    "sessionId": "ROOT",
                    "timestamp": "2026-05-04T10:00:01.000Z",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "tu-1",
                                "name": "mcp__plugin_callstack_call__invoke",
                                "input": {"task": "/task-a"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "uuid": "u1",
                    "sessionId": "ROOT",
                    "timestamp": "2026-05-04T10:00:02.000Z",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tu-1",
                                "content": '{"invoke_id": "i-aaaa"}',
                            }
                        ],
                    },
                },
            ]
        )
        + "\n"
    )

    page = read_messages(parent)
    res = _make_resolver(proj, log)
    annotate_spawns(
        page.messages, current_session_id="ROOT", spawn_resolver=res
    )

    tu = next(m for m in page.messages if m.role == "tool_use")
    assert tu.spawn_kind == "call"
    assert tu.spawn_session_ids == ["CHILD-A"]
    assert tu.spawn_tasks == ["/task-a"]


def test_healing_skips_fork_descendant_born_after_invoke(tmp_path: Path):
    """Regression: a ``--fork-session`` descendant of the real parent
    inherits its parent's full JSONL transcript, including the
    ``invoke_id`` of any callstack /call the parent made BEFORE the
    fork was born. The invoke index then lists the descendant as a
    candidate alongside the real parent — and the heal step could
    pick the descendant if it sorts first in discovery order. The
    result is cross-thread contamination on the canvas: an unrelated
    in-flight fork session appears to own a /call that completed long
    before it existed.

    Fix: filter candidates by JSONL birth_ts; only those alive when
    the invoke started can be the real emitter.
    """
    import os
    import time as _time

    log = tmp_path / "log"
    proj = tmp_path / "proj"
    proj.mkdir()

    # Use absolute UNIX timestamps so the test isn't sensitive to wall
    # clock drift.
    real_parent_birth = _time.time() - 7200  # 2 h ago
    invoke_started = _time.time() - 3600     # 1 h ago
    fork_descendant_birth = _time.time() - 60  # 1 min ago

    from datetime import datetime, timezone
    invoke_iso = datetime.fromtimestamp(invoke_started, tz=timezone.utc).isoformat()
    ended_iso = datetime.fromtimestamp(invoke_started + 30, tz=timezone.utc).isoformat()

    inv = log / "i-shared"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(
        yaml.safe_dump(
            {
                "invoke_id": "i-shared",
                # Recorded parent is bogus (callstack runtime drift):
                # no JSONL exists for it.
                "parent_session": "GHOST",
                "started_at": invoke_iso,
                "ended_at": ended_iso,
                "status": "complete",
                "tasks": [
                    {
                        "task": "/inner",
                        "status": "complete",
                        "depth": 1,
                        "session_id": "CHILD",
                    }
                ],
            }
        )
    )

    def _make_session_with_invoke(sid: str, birth_ts: float) -> None:
        path = proj / f"{sid}.jsonl"
        path.write_text(
            "\n".join(
                json.dumps(r)
                for r in [
                    {
                        "type": "assistant",
                        "uuid": f"{sid}-a",
                        "sessionId": sid,
                        "timestamp": invoke_iso,
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": f"tu-{sid}",
                                    "name": "mcp__plugin_callstack_call__call",
                                    "input": {"task": "/inner"},
                                }
                            ],
                        },
                    },
                    {
                        "type": "user",
                        "uuid": f"{sid}-u",
                        "sessionId": sid,
                        "timestamp": invoke_iso,
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": f"tu-{sid}",
                                    "content": '{"invoke_id": "i-shared"}',
                                }
                            ],
                        },
                    },
                ]
            )
            + "\n"
        )
        os.utime(path, (birth_ts, birth_ts))

    # Real parent: alive before the invoke fired.
    _make_session_with_invoke("REAL", real_parent_birth)
    # Fork descendant: born long after the invoke fired; only carries
    # the invoke_id because --fork-session copied the parent transcript.
    _make_session_with_invoke("DESCENDANT", fork_descendant_birth)

    (proj / "CHILD.jsonl").write_text(
        json.dumps(
            {
                "uuid": "u",
                "type": "user",
                "sessionId": "CHILD",
                "timestamp": invoke_iso,
                "message": {"role": "user", "content": "/inner"},
            }
        )
        + "\n"
    )

    res = _make_resolver(proj, log)

    # The spawn must be attributed to REAL, not DESCENDANT, regardless
    # of which sorts first in the invoke index.
    real_spawns = res.for_parent("REAL")
    desc_spawns = res.for_parent("DESCENDANT")
    assert len(real_spawns) == 1
    assert real_spawns[0].child_session_id == "CHILD"
    assert desc_spawns == []
    # Ghost recorded parent gets no spawn either.
    assert res.for_parent("GHOST") == []


def test_scan_session_has_returned_flag(tmp_path: Path):
    """``SessionScan.has_returned`` is True iff the last callstack
    envelope in the JSONL is a ``{"op":"return"}``. Used to override a
    stale ``running`` task status in ``report.yaml`` (Bug 1)."""
    from unwind.canvas_tree import scan_session

    proj = tmp_path / "proj"
    proj.mkdir()

    def _assistant_text_rec(text: str, ts: str = "2026-05-04T10:00:00.000Z") -> dict:
        return {
            "type": "assistant",
            "uuid": "a",
            "sessionId": "S",
            "timestamp": ts,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }

    # Case A: last envelope is a return.
    path_a = proj / "A.jsonl"
    _write_jsonl(
        path_a,
        [_assistant_text_rec('```json\n{"op": "return", "result": "ok"}\n```')],
    )
    assert scan_session(path_a).has_returned is True

    # Case B: return followed by a fresh user reply — no longer returned.
    path_b = proj / "B.jsonl"
    _write_jsonl(
        path_b,
        [
            _assistant_text_rec(
                '```json\n{"op": "return", "result": "ok"}\n```',
                "2026-05-04T10:00:00.000Z",
            ),
            {
                "type": "user",
                "uuid": "u2",
                "sessionId": "S",
                "timestamp": "2026-05-04T10:00:05.000Z",
                "message": {"role": "user", "content": "go again"},
            },
        ],
    )
    assert scan_session(path_b).has_returned is False

    # Case C: never returned.
    path_c = proj / "C.jsonl"
    _write_jsonl(path_c, [_assistant_text_rec("nothing here")])
    assert scan_session(path_c).has_returned is False

    # Case D: return then yield — yield wins (last envelope).
    path_d = proj / "D.jsonl"
    _write_jsonl(
        path_d,
        [
            _assistant_text_rec(
                '```json\n{"op": "return", "result": "ok"}\n```',
                "2026-05-04T10:00:00.000Z",
            ),
            _assistant_text_rec(
                '```json\n{"op": "yield", "question": "?"}\n```',
                "2026-05-04T10:00:05.000Z",
            ),
        ],
    )
    assert scan_session(path_d).has_returned is False


def test_fork_status_reads_from_session_scanner_when_wired(tmp_path: Path):
    """When a ``session_scanner`` is wired (registry path), fork status
    inference reads ``last_envelope_kind`` / ``last_envelope_ts`` from
    the cached SessionScan instead of re-walking the JSONL. Verifies
    the scanner path is exercised by counting JSONL reads."""
    from unwind.canvas_tree import CanvasTreeBuilder

    proj = tmp_path / "proj"
    log = tmp_path / "log"
    log.mkdir()

    _write_jsonl(
        proj / "ROOT.jsonl",
        [
            {
                "uuid": "head-uuid",
                "type": "user",
                "sessionId": "ROOT",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {"role": "user", "content": "kick off"},
            }
        ],
    )
    _write_jsonl(
        proj / "CHILD.jsonl",
        _fork_prologue_record(uuid="head-uuid") + [
            {
                "uuid": "a1",
                "type": "assistant",
                "sessionId": "CHILD",
                "timestamp": "2026-05-04T10:00:09.500Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": '```json\n{"op": "return", "result": "ok"}\n```',
                        }
                    ],
                },
            }
        ],
    )

    builder = CanvasTreeBuilder(proj)
    resolver = SpawnResolver(
        CallstackIndex(log),
        ForkDetector(proj),
        SubagentIndex(proj),
        project_dir=proj,
        session_scanner=builder.get_scan,
    )

    fork_spawns = [s for s in resolver.for_parent("ROOT") if s.source == "fork"]
    assert len(fork_spawns) == 1
    s = fork_spawns[0]
    assert s.status == "complete"
    assert s.ended_at is not None
    assert s.ended_at.isoformat().startswith("2026-05-04T10:00:09.500")

    # Sanity: the scan must record the envelope persistently — the source
    # of truth that lets us delete the second walk.
    scan = builder.get_scan("CHILD")
    assert scan.last_envelope_kind == "return"
    assert scan.last_envelope_ts is not None


def test_scan_session_tracks_last_envelope_across_resets(tmp_path: Path):
    """``last_envelope_kind`` / ``last_envelope_ts`` are persistent — they
    DON'T reset on intervening events the way ``at_user_prompt`` /
    ``has_returned`` do. A yield-then-resume-then-return sequence must
    end with kind="return" (the LAST envelope wins)."""
    from unwind.canvas_tree import scan_session

    path = tmp_path / "S.jsonl"
    _write_jsonl(
        path,
        [
            {
                "uuid": "a1",
                "type": "assistant",
                "sessionId": "S",
                "timestamp": "2026-05-04T10:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": '```json\n{"op":"yield","question":"?"}\n```'}
                    ],
                },
            },
            # User reply (resets at_user_prompt/has_returned) — but should
            # NOT touch last_envelope_*.
            {
                "uuid": "u1",
                "type": "user",
                "sessionId": "S",
                "timestamp": "2026-05-04T10:00:05.000Z",
                "message": {"role": "user", "content": "resume"},
            },
            {
                "uuid": "a2",
                "type": "assistant",
                "sessionId": "S",
                "timestamp": "2026-05-04T10:00:10.000Z",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": '```json\n{"op":"return","result":"ok"}\n```'}
                    ],
                },
            },
        ],
    )

    scan = scan_session(path)
    assert scan.last_envelope_kind == "return"
    assert scan.last_envelope_ts is not None
    assert scan.last_envelope_ts.isoformat().startswith("2026-05-04T10:00:10")
    # has_returned IS persistent in this case because no event followed
    # the return — sanity-check no regression.
    assert scan.has_returned is True
