"""Tests for ``unwind task`` verbs."""
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


def _scaffold_project(home: Path, tmp_path: Path) -> Path:
    real_cwd = tmp_path / "work" / "p"
    real_cwd.mkdir(parents=True)
    import unwind.projects as projects_mod
    importlib.reload(projects_mod)
    slug = projects_mod.slug_for(real_cwd)
    proj_dir = home / ".claude" / "projects" / slug
    _write_session(proj_dir, "ROOT", str(real_cwd))
    _write_session(proj_dir, "CHILD-A", str(real_cwd), head_uuid="diff-a")
    _write_session(proj_dir, "GRAND-B", str(real_cwd), head_uuid="diff-b")

    log_dir = real_cwd / ".claude" / "callstack" / "log"
    inv = log_dir / "20260101T000000-root"
    inv.mkdir(parents=True)
    (inv / "report.yaml").write_text(yaml.safe_dump({
        "invoke_id": "20260101T000000-root",
        "parent_session": "ROOT",
        "tasks": [
            {
                "task": "/task-a", "status": "complete", "depth": 1,
                "session_id": "CHILD-A",
                "children": [
                    {
                        "task": "/task-b", "status": "complete", "depth": 2,
                        "session_id": "GRAND-B",
                    },
                ],
            },
        ],
    }))
    return real_cwd


def test_task_tree_renders_callstack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = _scaffold_project(home, tmp_path)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["task", "tree", "ROOT", "--project", str(real_cwd), "--json"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["session_id"] == "ROOT"
    assert payload["children"][0]["session_id"] == "CHILD-A"
    assert payload["children"][0]["children"][0]["session_id"] == "GRAND-B"


def test_task_list_returns_direct_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = _scaffold_project(home, tmp_path)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["task", "list", "ROOT", "--project", str(real_cwd), "--json"],
    )
    assert result.exit_code == 0, result.output
    nodes = json.loads(result.output)
    assert len(nodes) == 1
    assert nodes[0]["session_id"] == "CHILD-A"


def test_task_roots_finds_root_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = _scaffold_project(home, tmp_path)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["task", "roots", "--project", str(real_cwd), "--json"],
    )
    assert result.exit_code == 0, result.output
    roots = json.loads(result.output)
    assert roots == ["ROOT"]


def test_task_kind_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup(tmp_path, monkeypatch)
    real_cwd = _scaffold_project(home, tmp_path)
    app = _reload_app()
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["task", "list", "ROOT", "--project", str(real_cwd), "--kind", "weird"],
    )
    assert result.exit_code == 2
