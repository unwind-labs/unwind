"""Thin shim over :mod:`unwind.settings` for the CLI-provided defaults.

Kept as a separate module so callers don't pull in the full settings surface;
they just want the default slug/path that the ``unwind serve`` CLI baked into
the environment before booting uvicorn.
"""
from __future__ import annotations

from typing import Optional

from .settings import get_settings


def default_slug() -> Optional[str]:
    return get_settings().default_slug


def default_source_path() -> Optional[str]:
    return get_settings().default_path
