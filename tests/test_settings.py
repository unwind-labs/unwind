"""Tests for the central Settings object."""
from __future__ import annotations

import pytest

from unwind.settings import (
    DEFAULT_DEV_ORIGINS,
    Settings,
    get_settings,
    init_settings,
    reset_settings,
)


@pytest.fixture(autouse=True)
def _isolate_settings():
    """Each test starts with no pinned settings and leaves none behind."""
    reset_settings()
    yield
    reset_settings()


def test_from_env_reads_all_known_vars(monkeypatch):
    monkeypatch.setenv("UNWIND_DEFAULT_SLUG", "my-slug")
    monkeypatch.setenv("UNWIND_DEFAULT_PATH", "/tmp/proj")
    monkeypatch.setenv("UNWIND_DOCS", "1")
    monkeypatch.setenv("UNWIND_ALLOWED_ORIGINS", "https://app.example")
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "tok")

    s = Settings.from_env()
    assert s.default_slug == "my-slug"
    assert s.default_path == "/tmp/proj"
    assert s.docs_enabled is True
    assert s.allowed_origins == ("https://app.example",)
    assert s.auth_token == "tok"


def test_from_env_defaults_when_unset(monkeypatch):
    for key in (
        "UNWIND_DEFAULT_SLUG",
        "UNWIND_DEFAULT_PATH",
        "UNWIND_DOCS",
        "UNWIND_ALLOWED_ORIGINS",
        "UNWIND_AUTH_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    s = Settings.from_env()
    assert s.default_slug is None
    assert s.default_path is None
    assert s.docs_enabled is False
    assert s.allowed_origins == DEFAULT_DEV_ORIGINS
    assert s.auth_token is None


def test_blank_envs_normalize_to_none(monkeypatch):
    monkeypatch.setenv("UNWIND_DEFAULT_SLUG", "   ")
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "")
    s = Settings.from_env()
    assert s.default_slug is None
    assert s.auth_token is None


def test_docs_truthy_variants(monkeypatch):
    for val in ("1", "true", "TRUE", "yes", "YES"):
        monkeypatch.setenv("UNWIND_DOCS", val)
        assert Settings.from_env().docs_enabled is True
    for val in ("0", "false", "no", "", "anything-else"):
        monkeypatch.setenv("UNWIND_DOCS", val)
        assert Settings.from_env().docs_enabled is False


def test_origins_reject_invalid_and_warn(monkeypatch, caplog):
    monkeypatch.setenv(
        "UNWIND_ALLOWED_ORIGINS",
        "null, *, example.com, https://ok.example",
    )
    with caplog.at_level("WARNING"):
        s = Settings.from_env()
    assert s.allowed_origins == ("https://ok.example",)
    # Three rejections should produce three warnings.
    rejections = [r for r in caplog.records if "rejecting invalid entry" in r.message]
    assert len(rejections) == 3


def test_get_settings_default_returns_fresh_env(monkeypatch):
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "first")
    assert get_settings().auth_token == "first"
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "second")
    assert get_settings().auth_token == "second"


def test_init_settings_pins_until_reset(monkeypatch):
    pinned = Settings(
        default_slug="pinned-slug",
        default_path=None,
        docs_enabled=False,
        allowed_origins=("https://pinned.example",),
        auth_token="pinned-token",
    )
    init_settings(pinned)
    # Env changes do not leak through while pinned.
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "env-token")
    assert get_settings() is pinned
    assert get_settings().auth_token == "pinned-token"
    reset_settings()
    assert get_settings().auth_token == "env-token"


def test_init_settings_without_arg_captures_env(monkeypatch):
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "captured")
    snapshot = init_settings()
    assert snapshot.auth_token == "captured"
    # Subsequent env changes do not affect the captured snapshot.
    monkeypatch.setenv("UNWIND_AUTH_TOKEN", "changed")
    assert get_settings().auth_token == "captured"
