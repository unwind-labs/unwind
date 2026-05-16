"""Central application settings.

One source of truth for all ``UNWIND_*`` environment variables. Code outside
this module should not read ``os.environ`` directly — call :func:`get_settings`
instead.

The CLI sets a few env vars (``UNWIND_DEFAULT_PATH``, ``UNWIND_DEFAULT_SLUG``)
just before ``uvicorn.run``; those land in the same process and are picked up
on the next ``get_settings()`` call. For explicit injection (tests, embedding),
call :func:`init_settings` with a constructed ``Settings``.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)


DEFAULT_DEV_ORIGINS: Tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
)


def _is_valid_origin_entry(entry: str) -> bool:
    """Strict allow-list validator: ``scheme://host[:port]``.

    Rejects ``null`` (sandboxed iframes / file:// pages), the wildcard ``*``,
    and any value missing a scheme or hostname.
    """
    lowered = entry.lower()
    if lowered in {"null", "*"}:
        return False
    parts = urlsplit(entry)
    if parts.scheme not in {"http", "https"}:
        return False
    if not parts.hostname:
        return False
    return True


def _parse_origins(raw: str) -> Tuple[str, ...]:
    raw = raw.strip()
    if not raw:
        return DEFAULT_DEV_ORIGINS
    out: list[str] = []
    for raw_entry in raw.split(","):
        entry = raw_entry.strip()
        if not entry:
            continue
        if not _is_valid_origin_entry(entry):
            logger.warning(
                "UNWIND_ALLOWED_ORIGINS: rejecting invalid entry %r "
                "(must be http(s)://host[:port], not 'null' or '*')",
                entry,
            )
            continue
        out.append(entry)
    return tuple(out)


def _env_str(name: str) -> Optional[str]:
    val = os.environ.get(name, "")
    val = val.strip()
    return val or None


def _env_bool(name: str) -> bool:
    return (os.environ.get(name, "").strip().lower()) in {"1", "true", "yes"}


@dataclass(frozen=True)
class Settings:
    """Resolved configuration. Built from env via :meth:`from_env` or injected."""

    default_slug: Optional[str]
    default_path: Optional[str]
    docs_enabled: bool
    allowed_origins: Tuple[str, ...]
    auth_token: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            default_slug=_env_str("UNWIND_DEFAULT_SLUG"),
            default_path=_env_str("UNWIND_DEFAULT_PATH"),
            docs_enabled=_env_bool("UNWIND_DOCS"),
            allowed_origins=_parse_origins(
                os.environ.get("UNWIND_ALLOWED_ORIGINS", "")
            ),
            auth_token=_env_str("UNWIND_AUTH_TOKEN"),
        )


_settings: Optional[Settings] = None


def get_settings() -> Settings:
    """Return the active settings.

    If :func:`init_settings` was called, that instance is returned. Otherwise,
    a fresh ``Settings.from_env()`` is built on each call so test code that
    monkeypatches env vars sees the change without bookkeeping.
    """
    if _settings is not None:
        return _settings
    return Settings.from_env()


def init_settings(settings: Optional[Settings] = None) -> Settings:
    """Pin the active settings (for explicit injection or a server boot).

    Pass ``None`` to capture the current env. Subsequent ``get_settings()``
    calls return this instance until :func:`reset_settings` is called.
    """
    global _settings
    _settings = settings if settings is not None else Settings.from_env()
    return _settings


def reset_settings() -> None:
    """Forget any pinned settings; ``get_settings()`` reverts to fresh env reads."""
    global _settings
    _settings = None
