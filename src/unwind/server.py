"""FastAPI app factory.

Serves the built frontend from ``src/unwind/static/`` and exposes the
``/api`` + ``/api/ws`` surface. During frontend development, the Vite dev server
runs separately on :5173 and proxies ``/api`` (including WebSocket) here.
"""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routers
from .events import get_bus
from .watcher import ensure_watcher, stop_all_watchers


STATIC_DIR = Path(__file__).parent / "static"


def _default_slug() -> str | None:
    return os.environ.get("UNWIND_DEFAULT_SLUG") or None


def _default_path() -> str | None:
    return os.environ.get("UNWIND_DEFAULT_PATH") or None


@asynccontextmanager
async def _lifespan(app: FastAPI):
    bus = get_bus()
    bus.bind_loop(asyncio.get_running_loop())
    slug = _default_slug()
    if slug:
        ensure_watcher(slug, bus)
    try:
        yield
    finally:
        stop_all_watchers()


def create_app() -> FastAPI:
    app = FastAPI(
        title="unwind",
        version=__version__,
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {
            "ok": True,
            "version": __version__,
            "default_slug": _default_slug(),
            "default_path": _default_path(),
        }

    for router in routers():
        app.include_router(router, prefix="/api")

    _mount_static(app)
    return app


def _mount_static(app: FastAPI) -> None:
    """Serve the Vite build output at the root, with SPA fallback."""
    if STATIC_DIR.is_dir() and (STATIC_DIR / "index.html").is_file():
        app.mount(
            "/assets",
            StaticFiles(directory=STATIC_DIR / "assets", check_dir=False),
            name="assets",
        )

        index_html = STATIC_DIR / "index.html"

        @app.get("/")
        def index() -> FileResponse:
            return FileResponse(index_html)

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # /api/* and /assets/* already matched above; this catches deep SPA
            # links like /projects/foo that should still render index.html.
            if full_path.startswith("api/") or full_path.startswith("assets/"):
                # 404 — FastAPI would already have matched a registered route.
                return FileResponse(index_html, status_code=404)
            target = STATIC_DIR / full_path
            if target.is_file():
                return FileResponse(target)
            return FileResponse(index_html)

    else:

        @app.get("/")
        def placeholder() -> JSONResponse:
            return JSONResponse(
                {
                    "status": "no-frontend-build",
                    "hint": (
                        "Build the frontend: `cd web && npm install && npm run build` "
                        "or run `npm run dev` separately during development."
                    ),
                }
            )
