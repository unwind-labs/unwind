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


def test_iter_lines_from_caps_at_max_bytes_and_truncates_at_newline(tmp_path: Path):
    """A huge append is consumed in multiple bounded reads, each ending on a newline."""
    p = tmp_path / "huge.jsonl"
    # Build a payload comfortably larger than the cap (use small cap for speed).
    cap = 4096
    line = json.dumps({"x": "a" * 200}) + "\n"
    n_lines = (cap * 3) // len(line)  # ~3x the cap
    p.write_text(line * n_lines)
    total_size = p.stat().st_size
    assert total_size > 2 * cap

    # First tick: capped — must read <= cap bytes and end at a newline.
    records1, off1 = iter_lines_from(p, 0, max_bytes=cap)
    got1 = list(records1)
    assert 0 < off1 <= cap
    # Last consumed byte must be a newline (we truncated at the last \n).
    with p.open("rb") as fh:
        fh.seek(off1 - 1)
        assert fh.read(1) == b"\n"
    assert len(got1) > 0
    assert all(r == {"x": "a" * 200} for r in got1)

    # Loop until drained; verify total record count matches the source.
    all_records = list(got1)
    off = off1
    while off < total_size:
        records, off = iter_lines_from(p, off, max_bytes=cap)
        all_records.extend(records)
    assert off == total_size
    assert len(all_records) == n_lines


def test_iter_lines_from_no_cap_when_under_threshold(tmp_path: Path):
    """Reads smaller than the cap consume to EOF in one call."""
    p = tmp_path / "small.jsonl"
    p.write_text('{"a":1}\n{"b":2}\n')
    records, off = iter_lines_from(p, 0, max_bytes=1024 * 1024)
    assert [r for r in records] == [{"a": 1}, {"b": 2}]
    assert off == p.stat().st_size


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
