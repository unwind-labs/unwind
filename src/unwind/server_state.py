"""Tiny module for reading CLI-provided env defaults at request time."""
from __future__ import annotations

import os
from typing import Optional


def default_slug() -> Optional[str]:
    return os.environ.get("UNWIND_DEFAULT_SLUG") or None


def default_source_path() -> Optional[str]:
    return os.environ.get("UNWIND_DEFAULT_PATH") or None
