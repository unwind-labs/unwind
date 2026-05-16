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

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .api import routers
from .events import get_bus
from .security import allowed_origins, auth_token, extract_bearer, is_token_valid
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


def _docs_enabled() -> bool:
    """OpenAPI docs are off unless UNWIND_DOCS=1.

    The default deployment binds to 127.0.0.1, so anyone with local access
    could browse the full API schema. We keep /api/docs available for
    development but require an explicit opt-in.
    """
    return os.environ.get("UNWIND_DOCS", "").strip() in {"1", "true", "yes"}


def create_app() -> FastAPI:
    docs_on = _docs_enabled()
    app = FastAPI(
        title="unwind",
        version=__version__,
        docs_url="/api/docs" if docs_on else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs_on else None,
        lifespan=_lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins(),
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    if auth_token() is not None:

        @app.middleware("http")
        async def _require_bearer(request: Request, call_next):
            # Static SPA assets bootstrap the page before any auth state exists;
            # they're inert HTML/JS/CSS, so they're exempted. /api/* and /ws are
            # the protected surface.
            path = request.url.path
            if path.startswith("/api/") or path.startswith("/ws"):
                token = extract_bearer(request.headers.get("authorization"))
                if not is_token_valid(token):
                    return JSONResponse(
                        {"detail": "unauthorized"}, status_code=401
                    )
            return await call_next(request)

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


def _safe_static_target(static_dir: Path, static_root: Path, rel: str) -> Path | None:
    """Resolve ``rel`` under ``static_dir`` only if the path is symlink-free and
    fully contained within the (resolved) static root.

    Returns the candidate Path on success or None on any rejection (caller
    falls through to index.html).
    """
    pre = static_dir / rel
    # Walk pre and each parent up to static_dir; refuse any symlinked component.
    # Stops at static_dir even if it is itself a symlink (operators may want to
    # mount static/ via a link; that's fine — we just don't follow links inside).
    check: Path = pre
    while True:
        if check.is_symlink():
            return None
        if check == static_dir:
            break
        parent = check.parent
        if parent == check:
            return None
        check = parent
    try:
        resolved = pre.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved != static_root and not resolved.is_relative_to(static_root):
        return None
    return resolved


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

        static_root = STATIC_DIR.resolve()

        @app.get("/{full_path:path}", include_in_schema=False)
        def spa(full_path: str) -> FileResponse:
            # /api/* and /assets/* already matched above; this catches deep SPA
            # links like /projects/foo that should still render index.html.
            if full_path.startswith("api/") or full_path.startswith("assets/"):
                # 404 — FastAPI would already have matched a registered route.
                return FileResponse(index_html, status_code=404)
            target = _safe_static_target(STATIC_DIR, static_root, full_path)
            if target is not None and target.is_file():
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
