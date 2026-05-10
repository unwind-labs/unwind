"""Tests for the unified spawn resolver."""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from unwind.callstack import CallstackIndex
from unwind.canvas_tree import build_canvas_tree
from unwind.fork_detect import ForkDetector
from unwind.messages import annotate_spawns, read_messages
from unwind.spawns import SpawnResolver
from unwind.subagents import SubagentIndex


# --- helpers -------------------------------------------------------------


def _make_resolver(project_dir: Path, log_dir: Path) -> SpawnResolver:
    return SpawnResolver(
        CallstackIndex(log_dir),
        ForkDetector(project_dir),
        SubagentIndex(project_dir),
        project_dir=project_dir,
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
