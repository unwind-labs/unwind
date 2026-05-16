"""Nonce + in-flight lock + timeout hardening for the folder-picker endpoint."""
from __future__ import annotations

import threading
import time
from unittest.mock import patch

from fastapi.testclient import TestClient

from unwind.api import projects as projects_api
from unwind.server import create_app


HEADERS = {"origin": "http://testserver", "host": "testserver"}


def _nonce(client: TestClient) -> str:
    r = client.get("/api/projects/pick-folder-nonce", headers=HEADERS)
    assert r.status_code == 200
    return r.json()["nonce"]


def test_post_without_nonce_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        # Missing body field → 422 (pydantic) before our handler runs.
        r = c.post("/api/projects/pick-folder", headers=HEADERS)
        assert r.status_code == 422


def test_post_with_unknown_nonce_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        r = c.post(
            "/api/projects/pick-folder",
            headers=HEADERS,
            json={"nonce": "not-a-real-token"},
        )
        assert r.status_code == 403


def test_nonce_is_single_use(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()
    with TestClient(app) as c:
        nonce = _nonce(c)
        with patch("unwind.api.projects.pick_folder", return_value=None):
            r1 = c.post(
                "/api/projects/pick-folder",
                headers=HEADERS,
                json={"nonce": nonce},
            )
        assert r1.status_code == 200
        # Replay of the same nonce must fail.
        r2 = c.post(
            "/api/projects/pick-folder",
            headers=HEADERS,
            json={"nonce": nonce},
        )
        assert r2.status_code == 403


def test_nonce_expires(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    # Shrink TTL so the test doesn't sleep for a real minute.
    monkeypatch.setattr(projects_api, "_NONCE_TTL", 0.05)
    app = create_app()
    with TestClient(app) as c:
        nonce = _nonce(c)
        time.sleep(0.1)
        r = c.post(
            "/api/projects/pick-folder",
            headers=HEADERS,
            json={"nonce": nonce},
        )
        assert r.status_code == 403


def test_concurrent_post_returns_409(tmp_path, monkeypatch):
    """Second concurrent request must get 409, not stack another modal."""
    monkeypatch.setenv("HOME", str(tmp_path))
    app = create_app()

    started = threading.Event()
    release = threading.Event()

    def slow_picker():
        started.set()
        # Block until the second request has had a chance to come in and
        # bounce off the in-flight lock.
        release.wait(timeout=2.0)
        return None

    with TestClient(app) as c:
        with patch("unwind.api.projects.pick_folder", side_effect=slow_picker):
            nonce_a = _nonce(c)
            nonce_b = _nonce(c)

            result: dict[str, int] = {}

            def first():
                r = c.post(
                    "/api/projects/pick-folder",
                    headers=HEADERS,
                    json={"nonce": nonce_a},
                )
                result["first"] = r.status_code

            t = threading.Thread(target=first)
            t.start()
            assert started.wait(timeout=2.0), "first request never invoked picker"

            r2 = c.post(
                "/api/projects/pick-folder",
                headers=HEADERS,
                json={"nonce": nonce_b},
            )
            assert r2.status_code == 409

            release.set()
            t.join(timeout=2.0)
            assert result.get("first") == 200


def test_dialog_timeout_is_120s():
    """The blocking subprocess must not stall the picker for 10 minutes."""
    import inspect

    from unwind import dialog

    osa_src = inspect.getsource(dialog._pick_with_osascript)
    tk_src = inspect.getsource(dialog._pick_with_tk)
    assert "timeout=120" in osa_src
    assert "timeout=120" in tk_src
    assert "timeout=600" not in osa_src
    assert "timeout=600" not in tk_src
