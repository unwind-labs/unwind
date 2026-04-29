from pathlib import Path
import json

from unwind.messages import read_messages


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
