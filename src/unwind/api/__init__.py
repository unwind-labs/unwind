"""FastAPI routers. Aggregated by ``routers()``."""
from __future__ import annotations

from fastapi import APIRouter

from . import canvas_export, projects, sessions_api, ws


def routers() -> list[APIRouter]:
    return [projects.router, sessions_api.router, ws.router, canvas_export.router]
