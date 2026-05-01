"""Tests for ``unwind session`` verbs."""
from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Optional

import pytest
import yaml
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


def _write_session(
    proj_dir: Path, sid: str, cwd: str, head_uuid: Optional[str] = None
) -> None:
    proj_dir.mkdir(parents=True, exist_ok=True)
    head = head_uuid or f"u-{sid}"
    (proj_dir / f"{sid}.jsonl").write_text(
        json.dumps({
            "type": "user",
            "uuid": head,
            "sessionId": sid,
            "timestamp": "2026-04-25T19:16:00.000Z",
            "cwd": cwd,
            "message": {"role": "user", "content": f"hello from {sid}"},
        }) + "\n"
    )


def test_session_list_returns_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "ROOT", str(real_cwd))
    _write_session(proj_dir, "OTHER", str(real_cwd))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app, ["session", "list", "--project", str(real_cwd), "--json"]
    )
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)
    sids = sorted(r["session_id"] for r in rows)
    assert sids == ["OTHER", "ROOT"]


def test_session_list_hides_callstack_forks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "ROOT", str(real_cwd))
    _write_session(proj_dir, "CHILD-A", str(real_cwd), head_uuid="other-uuid")

    log_dir = real_cwd / ".claude" / "callstack" / "log"
    inv = log_dir / "20260101T000000-root"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(yaml.safe_dump({
        "invoke_id": "20260101T000000-root",
        "parent_session": "ROOT",
        "tasks": [
            {"task": "/task-a", "status": "complete", "depth": 1, "session_id": "CHILD-A"},
        ],
    }))

    app = _reload_app()
    runner = CliRunner()
    # Default: forks hidden.
    result = runner.invoke(
        app, ["session", "list", "--project", str(real_cwd), "--json"]
    )
    assert result.exit_code == 0
    sids = sorted(r["session_id"] for r in json.loads(result.output))
    assert sids == ["ROOT"]

    # --include-forks: both visible.
    result = runner.invoke(
        app,
        ["session", "list", "--project", str(real_cwd), "--include-forks", "--json"],
    )
    assert result.exit_code == 0
    sids = sorted(r["session_id"] for r in json.loads(result.output))
    assert sids == ["CHILD-A", "ROOT"]


def test_session_show_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["session", "show", "no-such", "--project", str(real_cwd), "--json"],
    )
    assert result.exit_code == 1


def test_session_tree_shows_callstack_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "ROOT", str(real_cwd))
    _write_session(proj_dir, "CHILD-A", str(real_cwd), head_uuid="diff")

    log_dir = real_cwd / ".claude" / "callstack" / "log"
    inv = log_dir / "20260101T000000-root"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(yaml.safe_dump({
        "invoke_id": "20260101T000000-root",
        "parent_session": "ROOT",
        "tasks": [
            {"task": "/task-a", "status": "complete", "depth": 1, "session_id": "CHILD-A"},
        ],
    }))

    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["session", "tree", "ROOT", "--project", str(real_cwd), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session_id"] == "ROOT"
    assert any(c["session_id"] == "CHILD-A" for c in payload["children"])
