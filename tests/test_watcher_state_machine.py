"""Tests for the watcher's per-JSONL state machine.

Verifies the three branches in ``ProjectWatcher._handle_jsonl``:
  * unknown session   → ``_handle_new_session`` (emits session_created)
  * shrank            → ``_handle_shrink`` (invalidates + resets offset)
  * grew              → ``_handle_grew`` (emits messages_appended)
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from unwind.watcher import ProjectWatcher


def _write(path: Path, recs: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n")


def _user_rec(sid: str, uuid: str, ts: str, text: str) -> dict:
    return {
        "uuid": uuid,
        "type": "user",
        "sessionId": sid,
        "timestamp": ts,
        "message": {"role": "user", "content": text},
    }


class _Captor:
    """Capture EventBus emissions without spinning the WS layer."""

    def __init__(self) -> None:
        self.events: list = []

    def publish_threadsafe(self, ev) -> None:
        self.events.append(ev)


def test_new_session_branch_emits_session_created(tmp_path: Path, monkeypatch):
    """A JSONL appearing for an unknown session_id triggers
    ``_handle_new_session`` — session_created + messages_appended."""
    proj = tmp_path / "proj"
    sid = "S-NEW"
    _write(
        proj / f"{sid}.jsonl",
        [_user_rec(sid, "u1", "2026-05-04T10:00:00Z", "hello")],
    )

    # Stub index_for_slug to point at the project dir.
    from unwind import watcher as watcher_mod
    from unwind.sessions import SessionIndex
    from unwind.projects import ProjectPaths

    paths = ProjectPaths(
        slug="test",
        project_dir=proj,
        callstack_log_dir=tmp_path / "no-callstack",
        source_path=tmp_path,
    )
    monkeypatch.setattr(
        watcher_mod, "index_for_slug", lambda _slug: SessionIndex(paths)
    )

    bus = _Captor()
    w = ProjectWatcher("test", bus)  # type: ignore[arg-type]
    w._handle_jsonl(proj / f"{sid}.jsonl")

    kinds = [ev.type for ev in bus.events]
    assert "session_created" in kinds
    # messages_appended also lands so the UI doesn't need a second roundtrip.
    assert "messages_appended" in kinds
    assert sid in w._known_sessions


def test_shrink_branch_invalidates_cache(tmp_path: Path, monkeypatch):
    """A JSONL that shrank (size < recorded offset) invalidates the
    cached summary and resets the offset so the next call re-reads."""
    proj = tmp_path / "proj"
    sid = "S-SHRINK"
    path = proj / f"{sid}.jsonl"
    _write(path, [_user_rec(sid, "u1", "2026-05-04T10:00:00Z", "first")])

    from unwind import watcher as watcher_mod
    from unwind.sessions import SessionIndex
    from unwind.projects import ProjectPaths

    paths = ProjectPaths(
        slug="test",
        project_dir=proj,
        callstack_log_dir=tmp_path / "no-callstack",
        source_path=tmp_path,
    )
    index = SessionIndex(paths)
    monkeypatch.setattr(watcher_mod, "index_for_slug", lambda _slug: index)

    bus = _Captor()
    w = ProjectWatcher("test", bus)  # type: ignore[arg-type]

    # First call — establish known state, record a non-zero offset.
    w._handle_jsonl(path)
    initial_offset = w._offsets[sid]
    assert initial_offset > 0
    bus.events.clear()

    # Rewrite the file to be smaller — shrink path.
    path.write_text("")
    invalidate_called: list[str] = []
    monkeypatch.setattr(
        index, "invalidate", lambda s: invalidate_called.append(s)
    )

    w._handle_jsonl(path)
    assert invalidate_called == [sid]
    assert w._offsets[sid] == 0


def test_grew_branch_emits_messages_appended(tmp_path: Path, monkeypatch):
    """An already-known JSONL that grew triggers ``_handle_grew`` —
    only the NEW records since the prior offset get parsed and
    emitted, not the whole file."""
    proj = tmp_path / "proj"
    sid = "S-GREW"
    path = proj / f"{sid}.jsonl"
    _write(path, [_user_rec(sid, "u1", "2026-05-04T10:00:00Z", "first")])

    from unwind import watcher as watcher_mod
    from unwind.sessions import SessionIndex
    from unwind.projects import ProjectPaths

    paths = ProjectPaths(
        slug="test",
        project_dir=proj,
        callstack_log_dir=tmp_path / "no-callstack",
        source_path=tmp_path,
    )
    monkeypatch.setattr(
        watcher_mod, "index_for_slug", lambda _slug: SessionIndex(paths)
    )

    bus = _Captor()
    w = ProjectWatcher("test", bus)  # type: ignore[arg-type]

    # First call: register the session and capture its initial offset.
    w._handle_jsonl(path)
    bus.events.clear()
    assert sid in w._known_sessions

    # Append a second record (grow path).
    with path.open("a") as fh:
        fh.write(
            json.dumps(_user_rec(sid, "u2", "2026-05-04T10:00:30Z", "second")) + "\n"
        )
    # Ensure the file's mtime changes — some filesystems have 1s mtime
    # granularity which would make apply_increment skip the update.
    time.sleep(0.01)

    w._handle_jsonl(path)
    kinds = [ev.type for ev in bus.events]
    assert "messages_appended" in kinds
    # No second session_created — we already knew about this sid.
    assert "session_created" not in kinds
    appended = next(ev for ev in bus.events if ev.type == "messages_appended")
    # Only the NEW record makes it into the payload (not the original).
    new_msgs = appended.payload["messages"]
    assert len(new_msgs) == 1
    assert new_msgs[0]["uuid"] == "u2"
