"""Tests for ``--project`` resolution and ``--harness`` validation."""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _setup_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".claude" / "projects").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    # Also clear env defaults so tests don't see leaked CWD/SLUG from a real shell.
    monkeypatch.delenv("UNWIND_DEFAULT_PATH", raising=False)
    monkeypatch.delenv("UNWIND_DEFAULT_SLUG", raising=False)
    return home


def _reload_unwind():
    """Reload modules whose ``HOME`` lookups happen at import time."""
    import unwind.projects as projects_mod
    import unwind.registry as registry_mod
    importlib.reload(projects_mod)
    importlib.reload(registry_mod)


def _import_app():
    import unwind.cli as cli_mod
    importlib.reload(cli_mod)
    return cli_mod.app


def test_harness_codex_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _setup_home(tmp_path, monkeypatch)
    _reload_unwind()
    app = _import_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "list", "--harness", "codex"])
    assert result.exit_code == 2
    assert "not supported" in (result.stderr or result.output)


def test_bare_invocation_prints_help(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_home(tmp_path, monkeypatch)
    _reload_unwind()
    app = _import_app()
    runner = CliRunner()
    result = runner.invoke(app, [])
    # ``no_args_is_help=True`` returns exit code 0 (typer >=0.12) or 2 depending.
    assert result.exit_code in (0, 2)
    assert "serve" in result.output
    assert "project" in result.output


def test_project_list_json_empty_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_home(tmp_path, monkeypatch)
    _reload_unwind()
    app = _import_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "list", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []


def test_project_path_unknown_slug_returns_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_home(tmp_path, monkeypatch)
    _reload_unwind()
    app = _import_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "path", "no-such-slug"])
    assert result.exit_code == 1


def test_project_path_known_slug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = _setup_home(tmp_path, monkeypatch)
    proj = home / ".claude" / "projects" / "my-slug"
    proj.mkdir()
    _reload_unwind()
    app = _import_app()
    runner = CliRunner()
    result = runner.invoke(app, ["project", "path", "my-slug"])
    assert result.exit_code == 0, result.output
    assert str(proj) in result.output


def test_paths_for_serve_uses_existing_slug_when_pointed_at_project_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pointing unwind at ``~/.claude/projects/<slug>/`` must NOT re-slug it.

    Why: re-slugging the projects-dir path produces a doubled slug like
    ``-Users-me--claude-projects--<orig-slug>`` that doesn't match any real
    project directory, so the UI shows no sessions.
    """
    home = _setup_home(tmp_path, monkeypatch)
    slug = "-private-tmp-it-1165aef1"
    proj_dir = home / ".claude" / "projects" / slug
    proj_dir.mkdir()
    _reload_unwind()
    import unwind.cli as cli_mod
    importlib.reload(cli_mod)

    paths = cli_mod._paths_for_serve(proj_dir)
    assert paths.slug == slug
    assert paths.project_dir == proj_dir
    assert paths.has_project_dir is True


def test_paths_for_serve_normal_path_still_slugged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _setup_home(tmp_path, monkeypatch)
    _reload_unwind()
    import unwind.cli as cli_mod
    importlib.reload(cli_mod)

    src = tmp_path / "work" / "some-proj"
    src.mkdir(parents=True)
    paths = cli_mod._paths_for_serve(src)
    # Slug derived from the source path, not a "project dir" shortcut.
    assert paths.source_path == src
    assert paths.slug.endswith("-work-some-proj")
