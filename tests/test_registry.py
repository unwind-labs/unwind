"""Registry: ensure slug-only entry recovers the real cwd from a session
JSONL so callstack reports actually resolve."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _write_session(proj_dir: Path, sid: str, cwd: str) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps({
            "type": "user",
            "uuid": "u1",
            "sessionId": sid,
            "timestamp": "2026-04-25T19:16:00.000Z",
            "cwd": cwd,
            "message": {"role": "user", "content": "hi"},
        }) + "\n"
    )


def test_index_for_slug_upgrades_synthetic_path_using_session_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arrange: a fake $HOME with a Claude projects dir, a real project
    # cwd elsewhere with a .claude/callstack/log/ folder that callstack
    # reports would live in.
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    real_cwd = tmp_path / "work" / "myproj"
    (real_cwd / ".claude" / "callstack" / "log").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    # Reload the projects + registry modules so claude_projects_root() picks
    # up the patched HOME.
    import importlib
    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)

    slug = projects_mod.slug_for(real_cwd)
    proj_dir = projects_mod.claude_projects_root() / slug
    _write_session(proj_dir, "sess-A", str(real_cwd))

    # Act: ask the registry for this slug. The slug was never registered
    # as a real path, but the JSONL records its cwd — that's enough.
    index = registry_mod.index_for_slug(slug)

    # Assert: the index now points at the real cwd, and the callstack log
    # dir is the real one (not /dev/null/no-callstack).
    assert index.paths.source_path == real_cwd.resolve()
    assert index.paths.callstack_log_dir == (
        real_cwd.resolve() / ".claude" / "callstack" / "log"
    )

    ci = registry_mod.callstack_for_slug(slug)
    assert ci.has_logs is True

    # Cleanup: reload again so the patched HOME doesn't leak into other tests.
    monkeypatch.undo()
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)
