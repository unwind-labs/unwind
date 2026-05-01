"""Tests for ``unwind messages`` verbs."""
from __future__ import annotations

import importlib
import json
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


def _write_session(proj_dir: Path, sid: str, lines: list[dict]) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    with (proj_dir / f"{sid}.jsonl").open("w") as fh:
        for rec in lines:
            fh.write(json.dumps(rec) + "\n")


def test_messages_dump_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "S", [
        {
            "type": "user",
            "uuid": "u1",
            "sessionId": "S",
            "timestamp": "2026-04-25T19:16:00.000Z",
            "cwd": str(real_cwd),
            "message": {"role": "user", "content": "hello"},
        },
        {
            "type": "assistant",
            "uuid": "a1",
            "sessionId": "S",
            "timestamp": "2026-04-25T19:16:01.000Z",
            "message": {"role": "assistant", "content": [
                {"type": "text", "text": "hi back"},
            ]},
        },
    ])

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "messages", "dump", "S",
            "--project", str(real_cwd),
            "--format", "json",
        ],
    )
    assert result.exit_code == 0, result.output
    msgs = json.loads(result.output)
    roles = [m["role"] for m in msgs]
    assert "user" in roles and "assistant" in roles


def test_messages_dump_role_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "S", [
        {"type": "user", "uuid": "u1", "sessionId": "S",
         "timestamp": "2026-04-25T19:16:00.000Z", "cwd": str(real_cwd),
         "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "S",
         "timestamp": "2026-04-25T19:16:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]}},
    ])

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "messages", "dump", "S",
            "--project", str(real_cwd),
            "--format", "json",
            "--role", "user",
        ],
    )
    assert result.exit_code == 0, result.output
    msgs = json.loads(result.output)
    assert all(m["role"] == "user" for m in msgs)
    assert len(msgs) == 1


def test_messages_grep_finds_substring(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "S", [
        {"type": "user", "uuid": "u1", "sessionId": "S",
         "timestamp": "2026-04-25T19:16:00.000Z", "cwd": str(real_cwd),
         "message": {"role": "user", "content": "find this needle in the haystack"}},
        {"type": "assistant", "uuid": "a1", "sessionId": "S",
         "timestamp": "2026-04-25T19:16:01.000Z",
         "message": {"role": "assistant", "content": [{"type": "text", "text": "boring reply"}]}},
    ])

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "messages", "grep", "S", "needle",
            "--project", str(real_cwd),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "needle" in result.output
    assert "boring" not in result.output


def test_messages_dump_session_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["messages", "dump", "missing", "--project", str(real_cwd)],
    )
    assert result.exit_code == 1
