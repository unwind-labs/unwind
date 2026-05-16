"""Bearer-token middleware and non-loopback bind policy."""
from __future__ import annotations

import pytest
import typer
from fastapi.testclient import TestClient

from unwind.cli import _enforce_bind_policy
from unwind.security import extract_bearer, is_token_valid
from unwind.server import create_app


def test_enforce_bind_policy_loopback_ok(monkeypatch):
    monkeypatch.delenv("UNWIND_AUTH_TOKEN", raising=False)
    for host in ("127.0.0.1", "localhost", "::1"):
        _enforce_bind_policy(host)  # no raise


def test_enforce_bind_policy_non_loopback_without_token_raises(monkeypatch):
    monkeypatch.delenv("UNWIND_AUTH_TOKEN", raising=False)
    with pytest.raises(typer.Exit):
        _enforce_bind_policy("0.0.0.0")


def test_enforce_bind_policy_non_loopback_with_token_ok(monkeypatch):
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "s3cret")
    _enforce_bind_policy("0.0.0.0")


def test_extract_bearer_parses_well_formed():
    assert extract_bearer("Bearer abc123") == "abc123"
    assert extract_bearer("bearer XYZ") == "XYZ"


def test_extract_bearer_rejects_malformed():
    assert extract_bearer(None) is None
    assert extract_bearer("") is None
    assert extract_bearer("abc123") is None  # no scheme
    assert extract_bearer("Basic abc") is None  # wrong scheme
    assert extract_bearer("Bearer ") is None


def test_is_token_valid_constant_time(monkeypatch):
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "the-token")
    assert is_token_valid("the-token") is True
    assert is_token_valid("wrong") is False
    assert is_token_valid("") is False
    assert is_token_valid(None) is False


def test_is_token_valid_no_token_configured(monkeypatch):
    monkeypatch.delenv("UNWIND_AUTH_TOKEN", raising=False)
    assert is_token_valid("anything") is False


def test_api_without_token_returns_401_when_token_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "s3cret")
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects")
        assert r.status_code == 401


def test_api_with_token_passes_when_token_set(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "s3cret")
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects", headers={"Authorization": "Bearer s3cret"})
        assert r.status_code == 200


def test_api_with_wrong_token_returns_401(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "s3cret")
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401


def test_api_without_token_env_unchanged(tmp_path, monkeypatch):
    """Backward-compat: no UNWIND_AUTH_TOKEN ⇒ no auth required."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("UNWIND_AUTH_TOKEN", raising=False)
    app = create_app()
    with TestClient(app) as c:
        r = c.get("/api/projects")
        assert r.status_code == 200
