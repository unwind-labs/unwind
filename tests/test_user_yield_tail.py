"""``scan_session.at_user_prompt`` detects whether Claude is waiting on the user.

This used to live in ``_is_at_user_yield`` in ``sessions_api.py`` as a
separate state machine that read only the JSONL tail; both have been
consolidated onto the canvas scanner, which is mtime/size cached.
"""
from __future__ import annotations

import json
from pathlib import Path

from unwind.canvas_tree import scan_session


def _write_lines(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(l) for l in lines) + "\n")


def test_detects_yield_envelope(tmp_path: Path) -> None:
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
    assert scan_session(path).at_user_prompt is True


def test_detects_stop_hook_summary_as_yield(tmp_path: Path) -> None:
    """A system / stop_hook_summary record with no user reply afterwards
    means Claude wrapped its turn and is waiting for input.
    """
    path = tmp_path / "s.jsonl"
    _write_lines(path, [
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
        },
        {"type": "system", "subtype": "stop_hook_summary"},
    ])
    assert scan_session(path).at_user_prompt is True


def test_user_reply_after_yield_clears_state(tmp_path: Path) -> None:
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
    assert scan_session(path).at_user_prompt is False


def test_tool_result_does_not_clear_state(tmp_path: Path) -> None:
    """Tool-results are ``type: user`` records but Claude is still mid-turn —
    they must NOT clear the at-prompt flag set by the prior stop_hook_summary.
    """
    path = tmp_path / "s.jsonl"
    _write_lines(path, [
        {"type": "system", "subtype": "stop_hook_summary"},
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "ok"}
                ]
            },
        },
    ])
    assert scan_session(path).at_user_prompt is True


def test_missing_file_returns_false(tmp_path: Path) -> None:
    assert scan_session(tmp_path / "does-not-exist.jsonl").at_user_prompt is False
