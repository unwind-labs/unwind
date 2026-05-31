"""State-changing endpoints reject cross-origin requests."""
from __future__ import annotations

from unittest.mock import patch

from fastapi.testclient import TestClient

from unwind.server import create_app


def _same_origin_headers() -> dict[str, str]:
    return {"origin": "http://testserver", "host": "testserver"}


def _fetch_nonce(client) -> str:
    r = client.get(
        "/api/projects/pick-folder-nonce", headers=_same_origin_headers()
    )
    assert r.status_code == 200
    return r.json()["nonce"]


def test_pick_folder_rejects_cross_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_ALLOWED_ORIGINS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        r = c.post(
            "/api/projects/pick-folder",
            headers={"origin": "http://evil.example"},
            json={"nonce": "anything"},
        )
        assert r.status_code == 403


def test_pick_folder_accepts_same_origin(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        nonce = _fetch_nonce(c)
        # User cancels the picker dialog → endpoint must run, not be rejected.
        with patch("unwind.api.projects.pick_folder", return_value=None):
            r = c.post(
                "/api/projects/pick-folder",
                headers=_same_origin_headers(),
                json={"nonce": nonce},
            )
        assert r.status_code == 200
        assert r.json() == {"cancelled": True, "slug": None, "source_path": None}


def test_nonce_allows_missing_origin(tmp_path, monkeypatch):
    """Same-origin GETs omit the Origin header, so the nonce endpoint must
    not require it — otherwise the browser fetch that primes the picker 403s
    when the UI is served same-origin (the real-world bug)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_ALLOWED_ORIGINS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects/pick-folder-nonce")  # no Origin header
        assert r.status_code == 200
        assert r.json()["nonce"]


def test_nonce_rejects_cross_origin(tmp_path, monkeypatch):
    """An explicit foreign Origin is still rejected, so a cross-origin page
    can't grab a nonce."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_ALLOWED_ORIGINS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        r = c.get(
            "/api/projects/pick-folder-nonce",
            headers={"origin": "http://evil.example", "host": "testserver"},
        )
        assert r.status_code == 403


def test_pick_folder_rejects_no_origin(tmp_path, monkeypatch):
    """State-changing endpoints reject missing-Origin requests.

    A local non-browser process (any other CLI on the box, any subprocess of
    an installed app) could otherwise bypass the Origin-based CSRF guard.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_ALLOWED_ORIGINS", raising=False)
    app = create_app()
    with TestClient(app) as c:
        with patch("unwind.api.projects.pick_folder", return_value=None):
            r = c.post("/api/projects/pick-folder", json={"nonce": "anything"})
        assert r.status_code == 403
