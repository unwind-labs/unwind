"""_is_at_user_yield reads only the tail of large JSONLs."""
from __future__ import annotations

import json
from pathlib import Path

from unwind.api.sessions_api import _is_at_user_yield


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_detects_yield_in_short_file(tmp_path):
    path = tmp_path / "s.jsonl"
    _write_lines(path, [
        {"type": "user", "message": {"content": "hi"}},
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": '{"op": "yield"}'}]
            },
        },
    ])
    assert _is_at_user_yield(path) is True


def test_detects_yield_in_long_file_via_tail(tmp_path):
    """Tail read picks up the yield envelope even when the file is bigger
    than the tail window."""
    path = tmp_path / "s.jsonl"
    big_text = "x" * 100_000  # forces file > 64 KiB tail
    lines: list[dict] = [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": big_text}]},
        }
        for _ in range(2)
    ]
    # Final record carries the yield envelope; must be picked up.
    lines.append({
        "type": "assistant",
        "message": {
            "content": [{"type": "text", "text": '{"op": "yield"}'}]
        },
    })
    _write_lines(path, lines)
    assert path.stat().st_size > 64 * 1024
    assert _is_at_user_yield(path) is True


def test_user_reply_after_yield_clears_state(tmp_path):
    """Final non-tool_result user record means Claude is mid-turn again."""
    path = tmp_path / "s.jsonl"
    _write_lines(path, [
        {
            "type": "assistant",
            "message": {
                "content": [{"type": "text", "text": '{"op": "yield"}'}]
            },
        },
        {"type": "user", "message": {"content": "go on"}},
    ])
    assert _is_at_user_yield(path) is False


def test_missing_file_returns_false(tmp_path):
    assert _is_at_user_yield(tmp_path / "does-not-exist.jsonl") is False
