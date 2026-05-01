from pathlib import Path
import json

from unwind.messages import annotate_spawns, read_messages
from unwind.subagents import SubagentIndex


def _write(tmp: Path, lines: list[dict]) -> Path:
    p = tmp / "s.jsonl"
    with p.open("w") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
    return p


def test_pairs_tool_use_with_tool_result(tmp_path: Path):
    lines = [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "message": {"role": "user", "content": "run"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "sonnet",
                "content": [
                    {"type": "text", "text": "sure"},
                    {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"cmd": "ls"}},
                ],
            },
        },
        {
            "type": "user",
            "uuid": "u2",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:02.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                ],
            },
        },
    ]
    p = _write(tmp_path, lines)
    page = read_messages(p)
    roles = [m.role for m in page.messages]
    assert "user" in roles
    assert "assistant" in roles
    assert "tool_use" in roles
    assert "tool_result" in roles

    tool_use = [m for m in page.messages if m.role == "tool_use"][0]
    assert tool_use.tool_name == "Bash"
    assert tool_use.tool_input == {"cmd": "ls"}

    tool_result = [m for m in page.messages if m.role == "tool_result"][0]
    assert tool_result.tool_result_for == "t1"
    assert tool_result.tool_result == "ok"


def test_include_meta_toggles_attachments(tmp_path: Path):
    lines = [
        {
            "type": "attachment",
            "uuid": "att1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "attachment": {"hookName": "hook", "content": "hidden by default"},
        },
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {"role": "user", "content": "visible"},
        },
    ]
    p = _write(tmp_path, lines)
    no_meta = read_messages(p)
    assert [m.role for m in no_meta.messages] == ["user"]

    with_meta = read_messages(p, include_meta=True)
    roles = [m.role for m in with_meta.messages]
    assert roles == ["system", "user"]


def test_in_flight_agent_matches_pending_subagent(tmp_path: Path):
    """While an Agent tool_use is pending (no tool_result yet), unwind should
    still link it to the on-disk subagent trace so the UI can show the live
    subagent. Two sibling Agent calls with different descriptions get matched
    to the right traces, even though neither has a tool_result yet."""
    session_id = "sess-123"
    project_dir = tmp_path / "project"
    sub_dir = project_dir / session_id / "subagents"
    sub_dir.mkdir(parents=True)

    # Two pending subagents written by Claude Code.
    for agent_id, desc in (("aaaa1111", "Branch audit"), ("bbbb2222", "Code review")):
        (sub_dir / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": "general-purpose", "description": desc})
        )
        (sub_dir / f"agent-{agent_id}.jsonl").write_text("")

    # Parent session JSONL with two Agent tool_uses, no tool_results yet.
    parent_jsonl = project_dir / f"{session_id}.jsonl"
    parent_jsonl.write_text(
        json.dumps({
            "type": "assistant",
            "uuid": "a1",
            "sessionId": session_id,
            "timestamp": "2026-04-24T08:00:00.000Z",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Agent",
                     "input": {"description": "Branch audit", "prompt": "..."}},
                    {"type": "tool_use", "id": "t2", "name": "Agent",
                     "input": {"description": "Code review", "prompt": "..."}},
                ],
            },
        }) + "\n"
    )

    page = read_messages(parent_jsonl)
    si = SubagentIndex(project_dir)
    annotate_spawns(
        page.messages, current_session_id=session_id, subagent_index=si
    )

    tool_uses = [m for m in page.messages if m.role == "tool_use"]
    assert len(tool_uses) == 2

    by_id = {m.tool_use_id: m for m in tool_uses}
    assert by_id["t1"].spawn_kind == "subagent"
    assert by_id["t1"].spawn_session_ids == ["agent-aaaa1111"]
    assert by_id["t2"].spawn_kind == "subagent"
    assert by_id["t2"].spawn_session_ids == ["agent-bbbb2222"]
