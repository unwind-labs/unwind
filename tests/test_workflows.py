"""Tests for workflow ingestion: WorkflowIndex parsing, the workflow spawn
source in SpawnResolver, run anchoring, and the canvas subtree."""
from __future__ import annotations

import json
from pathlib import Path

from unwind.callstack import CallstackIndex
from unwind.canvas_tree import CanvasTreeBuilder, build_canvas_tree
from unwind.fork_detect import ForkDetector
from unwind.messages import annotate_spawns, read_messages
from unwind.spawns import SpawnResolver, WorkflowSpawn
from unwind.subagents import SubagentIndex
from unwind.workflows import WorkflowIndex


RUN_ID = "wf_test123-abc"
START_MS = 1_700_000_000_000


# --- fixtures ------------------------------------------------------------


def _agent_transcript(input_tokens: int, output_tokens: int) -> list[dict]:
    """A minimal agent JSONL with one priced assistant turn."""
    return [
        {
            "type": "user",
            "timestamp": "2026-05-04T10:00:00.000Z",
            "message": {"role": "user", "content": "do the thing"},
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-04T10:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "claude-opus-4-8",
                "id": "turn-1",
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
                "content": [{"type": "text", "text": "done"}],
            },
        },
    ]


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def _make_project(tmp_path: Path, *, with_rollup: bool = True) -> tuple[Path, str]:
    """Build a project dir with session ``main`` that launched one workflow.

    Lays down the rollup (unless ``with_rollup=False``, for the running
    case), the per-agent transcripts, and the journal.
    """
    proj = tmp_path / "proj"
    sid = "main"
    proj.mkdir(parents=True, exist_ok=True)

    # main session: a Workflow tool_use + its result echoing the run's
    # transcript dir (the form the run-id regex anchors on).
    transcript_dir = proj / sid / "subagents" / "workflows" / RUN_ID
    _write_jsonl(
        proj / f"{sid}.jsonl",
        [
            {
                "uuid": "u-launch",
                "type": "assistant",
                "sessionId": sid,
                "timestamp": "2026-05-04T09:59:00.000Z",
                "message": {
                    "role": "assistant",
                    "model": "claude-opus-4-8",
                    "id": "a-launch",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-wf",
                            "name": "Workflow",
                            "input": {"name": "deep-research"},
                        }
                    ],
                },
            },
            {
                "uuid": "u-result",
                "type": "user",
                "sessionId": sid,
                "timestamp": "2026-05-04T09:59:01.000Z",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-wf",
                            "content": (
                                "Workflow launched in background. Task ID: t1\n"
                                f"Transcript dir: {transcript_dir}\n"
                            ),
                        }
                    ],
                },
            },
        ],
    )

    # Three agents across two phases.
    agents = [
        ("aaaa1111", "scope", 1, "Scope", 100, 50),
        ("bbbb2222", "search:x", 2, "Search", 200, 80),
        ("cccc3333", "search:y", 2, "Search", 150, 60),
    ]
    for agent_id, _label, _pi, _pt, r_in, w_out in agents:
        _write_jsonl(
            transcript_dir / f"agent-{agent_id}.jsonl",
            _agent_transcript(r_in, w_out),
        )
        (transcript_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "workflow-subagent"})
        )

    # Journal: scope done, both search agents started (one done, one running).
    journal = transcript_dir / "journal.jsonl"
    journal.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"type": "started", "agentId": "aaaa1111"},
                {"type": "result", "agentId": "aaaa1111"},
                {"type": "started", "agentId": "bbbb2222"},
                {"type": "result", "agentId": "bbbb2222"},
                {"type": "started", "agentId": "cccc3333"},
            ]
        )
        + "\n"
    )

    if with_rollup:
        # Script (so degraded-name inference would work if needed).
        scripts = proj / sid / "workflows" / "scripts"
        scripts.mkdir(parents=True, exist_ok=True)
        (scripts / f"deep-research-{RUN_ID}.js").write_text("// script")

        progress: list[dict] = [
            {"type": "workflow_phase", "index": 1, "title": "Scope"},
            {"type": "workflow_phase", "index": 2, "title": "Search"},
        ]
        offset = 1000
        for agent_id, label, pi, pt, r_in, w_out in agents:
            progress.append(
                {
                    "type": "workflow_agent",
                    "agentId": agent_id,
                    "label": label,
                    "phaseIndex": pi,
                    "phaseTitle": pt,
                    "model": "claude-opus-4-8",
                    "state": "done",
                    "tokens": r_in + w_out,
                    "toolCalls": 2,
                    "startedAt": START_MS + offset,
                    "durationMs": 2000,
                }
            )
            offset += 1000
        rollup = proj / sid / "workflows" / f"{RUN_ID}.json"
        rollup.parent.mkdir(parents=True, exist_ok=True)
        rollup.write_text(
            json.dumps(
                {
                    "runId": RUN_ID,
                    "workflowName": "deep-research",
                    "status": "completed",
                    "startTime": START_MS,
                    "durationMs": 10_000,
                    "totalTokens": 640,
                    "result": {"summary": "all done"},
                    "logs": ["phase 1", "phase 2"],
                    "workflowProgress": progress,
                }
            )
        )

    return proj, sid


def _make_resolver(proj: Path) -> SpawnResolver:
    builder = CanvasTreeBuilder(proj)
    return SpawnResolver(
        CallstackIndex(proj / "no-log"),
        ForkDetector(proj, session_scanner=builder.get_scan),
        SubagentIndex(proj),
        project_dir=proj,
        workflows=WorkflowIndex(proj),
        session_scanner=builder.get_scan,
    )


# --- WorkflowIndex -------------------------------------------------------


def test_index_parses_rollup(tmp_path: Path):
    proj, sid = _make_project(tmp_path)
    wf = WorkflowIndex(proj)
    assert wf.parent_sids() == {sid}
    runs = wf.list_for_session(sid)
    assert len(runs) == 1
    run = runs[0]
    assert run.run_id == RUN_ID
    assert run.name == "deep-research"
    assert run.status == "completed"
    assert run.partial is False
    assert [p.title for p in run.phases] == ["Scope", "Search"]
    assert len(run.agents) == 3
    assert {a.agent_id for a in run.agents} == {"aaaa1111", "bbbb2222", "cccc3333"}
    assert run.agents[0].started_at is not None


def test_index_running_run_is_partial(tmp_path: Path):
    """A run with a transcript dir but no rollup is synthesised, with agent
    done/running derived from the journal."""
    proj, sid = _make_project(tmp_path, with_rollup=False)
    wf = WorkflowIndex(proj)
    runs = wf.list_for_session(sid)
    assert len(runs) == 1
    run = runs[0]
    assert run.partial is True
    assert run.status == "running"
    states = {a.agent_id: a.state for a in run.agents}
    assert states["aaaa1111"] == "done"
    assert states["bbbb2222"] == "done"
    assert states["cccc3333"] == "running"  # started, no result yet


def test_subagent_index_resolves_workflow_agent_transcript(tmp_path: Path):
    """The nested workflow-agent transcript resolves through the same
    ``agent-<id>`` path the canvas/messages endpoints use."""
    proj, _sid = _make_project(tmp_path)
    sa = SubagentIndex(proj)
    path = sa.resolve("agent-bbbb2222")
    assert path is not None
    assert path.name == "agent-bbbb2222.jsonl"
    assert "workflows" in path.parts


# --- spawn source --------------------------------------------------------


def test_resolver_emits_run_phase_agent(tmp_path: Path):
    proj, sid = _make_project(tmp_path)
    res = _make_resolver(proj)
    by_parent = res.spawns_by_parent()

    # Run node is a child of the launching session.
    runs = [s for s in by_parent[sid] if isinstance(s, WorkflowSpawn)]
    assert len(runs) == 1 and runs[0].node_role == "run"
    assert runs[0].child_session_id == RUN_ID

    # Phase nodes are children of the run node.
    phases = [s for s in by_parent[RUN_ID] if isinstance(s, WorkflowSpawn)]
    assert {s.node_role for s in phases} == {"phase"}
    assert len(phases) == 2

    # Agents hang off their phase node, addressed as agent-<id>.
    phase2 = f"{RUN_ID}::p2"
    agents = [s for s in by_parent[phase2] if isinstance(s, WorkflowSpawn)]
    assert {s.node_role for s in agents} == {"agent"}
    assert {s.child_session_id for s in agents} == {"agent-bbbb2222", "agent-cccc3333"}


def test_run_anchors_to_workflow_tool_use(tmp_path: Path):
    proj, sid = _make_project(tmp_path)
    res = _make_resolver(proj)
    page = read_messages(proj / f"{sid}.jsonl")
    anchored = res.anchor_to_messages(sid, page.messages)
    runs = [s for s in anchored if isinstance(s, WorkflowSpawn) and s.node_role == "run"]
    assert len(runs) == 1
    assert runs[0].parent_tool_use_id == "tu-wf"

    # And the tool_use row is decorated as a workflow spawn.
    annotate_spawns(page.messages, current_session_id=sid, spawn_resolver=res)
    wf_rows = [m for m in page.messages if m.role == "tool_use" and m.spawn_kind == "workflow"]
    assert len(wf_rows) == 1
    assert wf_rows[0].spawn_session_ids == [RUN_ID]


# --- canvas tree ---------------------------------------------------------


def test_canvas_nests_and_rolls_up_tokens(tmp_path: Path):
    proj, sid = _make_project(tmp_path)
    res = _make_resolver(proj)
    builder = CanvasTreeBuilder(proj)
    root, all_windows = build_canvas_tree(
        proj, sid, spawn_resolver=res, builder=builder
    )

    run = next(w for w in all_windows if w.kind == "workflow")
    assert run.session_id == RUN_ID
    phase_nodes = [w for w in all_windows if w.kind == "workflow_phase"]
    assert len(phase_nodes) == 2
    # Agent leaves reuse the subagent kind (real transcript + drill-in).
    agent_nodes = [
        w for w in all_windows if w.kind == "subagent" and w.session_id.startswith("agent-")
    ]
    assert len(agent_nodes) == 3

    # The run is nested under the root (launching) session.
    assert run.parent_window_id == root.window_id

    # Agent tokens (100+50, 200+80, 150+60 = 640) roll up into the run's
    # subtree usage; the run's own self usage is zero (no transcript).
    assert sum(run.self_usage.values()) == 0
    assert sum(run.subtree_usage.values()) == 640
    # And up into the root.
    assert sum(root.subtree_usage.values()) == 640
    assert sum(run.subtree_cost.values()) > 0
