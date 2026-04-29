import json
from pathlib import Path

from unwind.jsonl import extract_session_summary, iter_lines, iter_lines_from


def _write_jsonl(tmp: Path, lines: list[dict]) -> Path:
    p = tmp / "sess.jsonl"
    with p.open("w") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")
    return p


def test_iter_lines_skips_malformed(tmp_path: Path):
    p = tmp_path / "mix.jsonl"
    p.write_text('{"a":1}\nnot json\n{"b":2}\n')
    out = list(iter_lines(p))
    assert out == [{"a": 1}, {"b": 2}]


def test_extract_summary_counts_messages(tmp_path: Path):
    lines = [
        {"type": "permission-mode", "permissionMode": "auto"},
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:00.000Z",
            "cwd": "/tmp/proj",
            "gitBranch": "main",
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "attachment",
            "uuid": "att",
            "sessionId": "s",
            "attachment": {"hookName": "h"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "s",
            "timestamp": "2026-04-24T08:00:01.000Z",
            "message": {
                "role": "assistant",
                "model": "sonnet",
                "content": [{"type": "text", "text": "hi"}],
            },
        },
    ]
    p = _write_jsonl(tmp_path, lines)
    summary = extract_session_summary(p, "s")
    assert summary is not None
    assert summary.session_id == "s"
    assert summary.title == "hello"
    assert summary.message_count == 2
    assert summary.cwd == "/tmp/proj"
    assert summary.git_branch == "main"
    assert summary.first_timestamp is not None
    assert summary.last_timestamp is not None


def test_iter_lines_from_tail(tmp_path: Path):
    p = tmp_path / "grow.jsonl"
    p.write_text('{"n":1}\n{"n":2}\n')
    size = p.stat().st_size
    with p.open("a") as fh:
        fh.write('{"n":3}\n{"n":4}\n')
    records, new_offset = iter_lines_from(p, size)
    got = list(records)
    assert [r["n"] for r in got] == [3, 4]
    assert new_offset == p.stat().st_size


def test_title_truncation(tmp_path: Path):
    long_text = "x" * 400
    p = _write_jsonl(
        tmp_path,
        [
            {
                "type": "user",
                "uuid": "u",
                "sessionId": "s",
                "timestamp": "2026-04-24T08:00:00.000Z",
                "message": {"role": "user", "content": long_text},
            }
        ],
    )
    summary = extract_session_summary(p, "s")
    assert summary is not None
    assert summary.title.endswith("…")
    assert len(summary.title) <= 140
