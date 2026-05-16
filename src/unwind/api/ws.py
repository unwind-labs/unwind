"""WebSocket endpoint: subscribes a client to live events for a project slug."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..events import get_bus
from ..security import (
    auth_token,
    extract_bearer,
    is_origin_allowed,
    is_token_valid,
    is_valid_slug,
)
from ..watcher import ensure_watcher

log = logging.getLogger("unwind.ws")

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, project: str = "", token: str = "") -> None:
    origin = ws.headers.get("origin")
    host = ws.headers.get("host")
    secure = ws.url.scheme == "wss"
    if not is_origin_allowed(origin, host, secure=secure):
        # Reject without ACCEPT so the browser sees the handshake fail.
        log.warning("ws origin rejected origin=%s host=%s", origin, host)
        await ws.close(code=1008)
        return

    if auth_token() is not None:
        # Browsers can't set Authorization headers on the WS handshake; accept
        # the token via Sec-WebSocket-Protocol or ?token= query.
        presented = (
            extract_bearer(ws.headers.get("authorization"))
            or token.strip()
            or None
        )
        if not is_token_valid(presented):
            log.warning("ws auth rejected origin=%s", origin)
            await ws.close(code=1008)
            return

    await ws.accept()
    if not project:
        await ws.send_json({"type": "error", "error": "missing ?project="})
        await ws.close()
        return
    if not is_valid_slug(project):
        await ws.send_json({"type": "error", "error": "invalid project slug"})
        await ws.close()
        return

    bus = get_bus()
    bus.bind_loop(asyncio.get_running_loop())
    ensure_watcher(project, bus)
    queue = await bus.subscribe(project)

    await ws.send_json({"type": "ready", "slug": project})

    try:
        while True:
            # Race: incoming client msg vs outbound event.
            recv_task: asyncio.Task = asyncio.create_task(ws.receive_text())
            send_task: asyncio.Task = asyncio.create_task(queue.get())
            done, pending = await asyncio.wait(
                {recv_task, send_task}, return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if recv_task in done:
                try:
                    msg = recv_task.result()
                except WebSocketDisconnect:
                    break
                if msg == "ping":
                    await ws.send_json({"type": "pong"})
            if send_task in done:
                try:
                    event = send_task.result()
                except asyncio.CancelledError:
                    continue
                await ws.send_json(event.to_dict())
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("ws handler crashed slug=%s", project)
    finally:
        await bus.unsubscribe(project, queue)
        try:
            await ws.close()
        except Exception:
            pass
