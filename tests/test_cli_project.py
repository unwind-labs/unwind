"""Tests for ``unwind project`` verbs."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UNWIND_DEFAULT_PATH", raising=False)
    monkeypatch.delenv("UNWIND_DEFAULT_SLUG", raising=False)
    return home


def _reload_app():
    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    import unwind.cli as cli_mod
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)
    importlib.reload(cli_mod)
    return cli_mod.app


def _write_session(proj_dir: Path, sid: str, cwd: str) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps({
            "type": "user",
            "uuid": f"u-{sid}",
            "sessionId": sid,
            "timestamp": "2026-04-25T19:16:00.000Z",
            "cwd": cwd,
            "message": {"role": "user", "content": f"hello from {sid}"},
        }) + "\n"
    )


def test_project_list_with_one_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "myproj"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "sess-A", str(real_cwd))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["slug"] == slug
    assert rows[0]["session_count"] == 1


def test_project_show_by_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "sA", str(real_cwd))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "show", "--slug", slug, "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["slug"] == slug
    assert payload["session_count"] == 1


def test_project_path_resolves_known_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    proj = home / ".claude" / "projects" / "abc"
    proj.mkdir()
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "path", "abc"])
    assert result.exit_code == 0
    assert str(proj) in result.output


def test_project_path_unknown_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "path", "no-such"])
    assert result.exit_code == 1


def test_project_list_excludes_forks_from_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``unwind project list`` reports the visible (fork-hidden) session count,
    matching what ``unwind session list`` shows in the same project."""
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "fork-proj"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    proj_dir.mkdir(parents=True, exist_ok=True)

    # Parent + one callstack-fork session sharing the same head uuid.
    parent_sid = "parent-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    fork_sid = "forkk-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
    head = {
        "uuid": "head-shared",
        "type": "user",
        "sessionId": parent_sid,
        "timestamp": "2026-04-24T09:00:00.000Z",
        "cwd": str(real_cwd),
        "message": {"role": "user", "content": "shared prefix"},
    }
    parent_path = proj_dir / f"{parent_sid}.jsonl"
    parent_path.write_text(
        "\n".join([
            json.dumps(head),
            json.dumps({
                "uuid": "p-2",
                "type": "assistant",
                "sessionId": parent_sid,
                "timestamp": "2026-04-24T09:00:01.000Z",
                "message": {"role": "assistant", "content": "ok"},
            }),
        ]) + "\n"
    )
    fork_path = proj_dir / f"{fork_sid}.jsonl"
    fork_path.write_text(
        "\n".join([
            # The callstack runtime injects this prologue into a forked
            # child's first queue-op enqueue. ``ForkDetector`` keys off
            # the prefix to classify the session as a fork.
            json.dumps({
                "type": "queue-operation",
                "operation": "enqueue",
                "content": "You are running in a forked session — execute /task-x",
                "timestamp": "2026-04-24T10:00:00.000Z",
            }),
            json.dumps(head),  # inherited
            json.dumps({
                "uuid": "fork-own-0",
                "type": "user",
                "sessionId": fork_sid,
                "timestamp": "2026-04-24T10:00:00.000Z",
                "cwd": str(real_cwd),
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "/task-x"}],
                },
            }),
        ]) + "\n"
    )
    # Parent must be older than fork for the family-root selection.
    os.utime(parent_path, (1700000000, 1700000000))
    os.utime(fork_path, (1700000100, 1700000100))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "list", "--json"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    assert len(rows) == 1
    assert rows[0]["slug"] == slug
    # 1 root + 1 fork = 2 sessions on disk; visible count must be 1.
    assert rows[0]["session_count"] == 1


def test_project_current_uses_env_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "sA", str(real_cwd))
    monkeypatch.setenv("UNWIND_DEFAULT_PATH", str(real_cwd))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "current", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["slug"] == slug
