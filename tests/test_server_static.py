"""SPA static fallback must not serve files outside STATIC_DIR."""
from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from unwind import server as server_mod


def _setup_static(tmp_path: Path, monkeypatch) -> Path:
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text("<html>spa</html>")
    (static / "favicon.ico").write_text("ICO")
    (static / "assets" / "app.js").write_text("// js")
    monkeypatch.setattr(server_mod, "STATIC_DIR", static)
    return static


def test_spa_serves_real_file(tmp_path, monkeypatch):
    _setup_static(tmp_path, monkeypatch)
    app = server_mod.create_app()
    with TestClient(app) as c:
        r = c.get("/favicon.ico")
        assert r.status_code == 200
        assert r.text == "ICO"


def test_spa_falls_back_to_index_for_unknown(tmp_path, monkeypatch):
    _setup_static(tmp_path, monkeypatch)
    app = server_mod.create_app()
    with TestClient(app) as c:
        r = c.get("/projects/whatever")
        assert r.status_code == 200
        assert "spa" in r.text


def test_spa_rejects_symlink_to_file_outside(tmp_path, monkeypatch):
    """A symlink inside static/ pointing outside must not leak its target."""
    static = _setup_static(tmp_path, monkeypatch)
    outside = tmp_path / "secret.txt"
    outside.write_text("TOPSECRET")
    (static / "leak").symlink_to(outside)

    app = server_mod.create_app()
    with TestClient(app) as c:
        r = c.get("/leak")
        assert r.status_code == 200
        assert "TOPSECRET" not in r.text
        assert "spa" in r.text


def test_spa_rejects_symlink_dir_component(tmp_path, monkeypatch):
    """A symlinked directory component must not let nested files leak."""
    static = _setup_static(tmp_path, monkeypatch)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    (outside_dir / "secret.txt").write_text("TOPSECRET")
    (static / "linkdir").symlink_to(outside_dir)

    app = server_mod.create_app()
    with TestClient(app) as c:
        r = c.get("/linkdir/secret.txt")
        assert r.status_code == 200
        assert "TOPSECRET" not in r.text
        assert "spa" in r.text


def test_spa_rejects_traversal_via_resolved_target(tmp_path, monkeypatch):
    """Even if path-resolution lands outside STATIC_DIR, only index.html is served."""
    static = _setup_static(tmp_path, monkeypatch)
    outside = tmp_path / "secret.txt"
    outside.write_text("TOPSECRET")

    # Confirm the resolved target really would escape without the guard.
    assert (static / "../secret.txt").resolve() == outside

    app = server_mod.create_app()
    with TestClient(app) as c:
        # Starlette normalises "../" in URLs, but the route also accepts
        # `full_path` values containing "..". Hit the SPA handler directly via
        # a backslash-encoded path that bypasses URL normalisation in some
        # clients but is still passed into our handler as raw text.
        r = c.get("/..%2Fsecret.txt")
        assert r.status_code == 200
        assert "TOPSECRET" not in r.text
        assert "spa" in r.text
