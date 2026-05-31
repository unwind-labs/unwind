"""SessionIndex.apply_increment avoids the full JSONL re-parse on tail growth."""
from __future__ import annotations

import json
from pathlib import Path

from unwind import jsonl as jsonl_mod
from unwind.projects import ProjectPaths
from unwind.sessions import SessionIndex


def _write(p: Path, recs: list[dict]) -> None:
    with p.open("a") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


def _paths(tmp: Path) -> ProjectPaths:
    return ProjectPaths(
        slug="t",
        source_path=tmp,
        project_dir=tmp,
        callstack_log_dir=tmp / "callstack",
    )


def test_apply_increment_updates_without_re_parsing(tmp_path: Path, monkeypatch):
    jsonl_path = tmp_path / "abc.jsonl"
    _write(
        jsonl_path,
        [
            {"type": "user", "timestamp": "2026-05-16T01:00:00Z",
             "message": {"role": "user", "content": "first"},
             "cwd": "/repo", "gitBranch": "main"},
            {"type": "assistant", "timestamp": "2026-05-16T01:00:05Z",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "hi"}]}},
        ],
    )
    idx = SessionIndex(_paths(tmp_path))

    # Cold start — full parse populates cache.
    s0 = idx.get_session("abc")
    assert s0 is not None
    assert s0.message_count == 2
    assert s0.cwd == "/repo"
    assert s0.git_branch == "main"

    # Count full-parse calls; the next path must not invoke this.
    calls = {"n": 0}
    real = jsonl_mod.extract_session_summary

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(jsonl_mod, "extract_session_summary", counting)
    # SessionIndex imports extract_session_summary by name — patch its
    # reference too.
    from unwind import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "extract_session_summary", counting)

    # Append a record, then drive the incremental path.
    new_recs = [
        {"type": "user", "timestamp": "2026-05-16T01:01:00Z",
         "message": {"role": "user", "content": "second"}},
    ]
    _write(jsonl_path, new_recs)
    st = jsonl_path.stat()

    s1 = idx.apply_increment("abc", new_recs, st.st_size, st.st_mtime)

    assert calls["n"] == 0, "apply_increment must not call extract_session_summary"
    assert s1 is not None
    assert s1.message_count == 3
    assert s1.cwd == "/repo"  # carried from cold start
    assert s1.git_branch == "main"
    assert s1.last_timestamp is not None
    assert s1.last_timestamp.isoformat().startswith("2026-05-16T01:01:00")

    # Subsequent get_session should hit the updated cache (mtime/size match).
    s2 = idx.get_session("abc")
    assert s2 is s1
    assert calls["n"] == 0


def test_apply_increment_collapses_turn_straddling_boundary(tmp_path: Path):
    """A single assistant turn is block-split across records; if the cold
    parse ends mid-turn and the rest is appended, the increment must NOT
    count the turn a second time. The continuity token (``last_assistant_id``)
    carried on the summary is what prevents the double-count."""
    jsonl_path = tmp_path / "abc.jsonl"
    _write(
        jsonl_path,
        [
            {"type": "user", "timestamp": "2026-05-16T01:00:00Z",
             "message": {"role": "user", "content": "go"}},
            # First two blocks of turn msg_A land before the read boundary.
            {"type": "assistant", "timestamp": "2026-05-16T01:00:05Z",
             "requestId": "req_A",
             "message": {"id": "msg_A", "role": "assistant",
                         "content": [{"type": "thinking"}]}},
            {"type": "assistant", "timestamp": "2026-05-16T01:00:05Z",
             "requestId": "req_A",
             "message": {"id": "msg_A", "role": "assistant",
                         "content": [{"type": "text"}]}},
        ],
    )
    idx = SessionIndex(_paths(tmp_path))
    s0 = idx.get_session("abc")
    assert s0 is not None
    assert s0.message_count == 2  # 1 user + 1 assistant turn
    assert s0.last_assistant_id == "msg_A"

    # Append the REST of the same turn (more blocks of msg_A) + a new turn.
    new_recs = [
        {"type": "assistant", "timestamp": "2026-05-16T01:00:06Z",
         "requestId": "req_A",
         "message": {"id": "msg_A", "role": "assistant",
                     "content": [{"type": "tool_use"}]}},
        {"type": "user", "timestamp": "2026-05-16T01:00:07Z",
         "message": {"role": "user",
                     "content": [{"type": "tool_result", "content": "x"}]}},
        {"type": "assistant", "timestamp": "2026-05-16T01:00:08Z",
         "requestId": "req_B",
         "message": {"id": "msg_B", "role": "assistant",
                     "content": [{"type": "text"}]}},
    ]
    _write(jsonl_path, new_recs)
    st = jsonl_path.stat()
    s1 = idx.apply_increment("abc", new_recs, st.st_size, st.st_mtime)
    assert s1 is not None
    # msg_A's straddling block must NOT re-count; tool_result must not count;
    # only the new msg_B turn adds 1 → total 3.
    assert s1.message_count == 3
    assert s1.last_assistant_id == "msg_B"

    # A fresh full parse must agree with the incremental count (no drift).
    fresh = SessionIndex(_paths(tmp_path)).get_session("abc")
    assert fresh is not None
    assert fresh.message_count == 3


def test_apply_increment_returns_none_on_cache_miss(tmp_path: Path):
    """No cached entry → caller must fall back to a full parse."""
    idx = SessionIndex(_paths(tmp_path))
    result = idx.apply_increment("nope", [], 0, 0.0)
    assert result is None


def test_apply_increment_updates_custom_title(tmp_path: Path):
    jsonl_path = tmp_path / "xyz.jsonl"
    _write(
        jsonl_path,
        [{"type": "user", "timestamp": "2026-05-16T02:00:00Z",
          "message": {"role": "user", "content": "hello"}}],
    )
    idx = SessionIndex(_paths(tmp_path))
    s0 = idx.get_session("xyz")
    assert s0 is not None
    assert s0.custom_title is None

    new_recs = [{"type": "custom-title", "customTitle": "  My Session  "}]
    _write(jsonl_path, new_recs)
    st = jsonl_path.stat()

    s1 = idx.apply_increment("xyz", new_recs, st.st_size, st.st_mtime)
    assert s1 is not None
    assert s1.custom_title == "My Session"
    assert s1.title == "My Session"
    assert s1.message_count == 1  # custom-title is not a message
