"""WebSocket endpoint: subscribes a client to live events for a project slug."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..events import get_bus
from ..watcher import ensure_watcher

log = logging.getLogger("unwind.ws")

router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket, project: str = "") -> None:
    await ws.accept()
    if not project:
        await ws.send_json({"type": "error", "error": "missing ?project="})
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
