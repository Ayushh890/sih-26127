"""Browser Local Camera ingestion ``/ws/cameras/{camera_id}/ingest?token=…``.

The console's Local Camera page captures the laptop webcam and sends each frame as one
binary WebSocket message (a JPEG). Frames are put into the camera's bounded input inbox
on the event bus, where :class:`app.camera.sources.browser.BrowserPushSource` hands them
to the normal stream worker — detection, tracking, OCR and ingestion are the same as for
RTSP. Requires ``cameras:control`` and a camera whose ``source_type`` is ``browser``.

Flow control: the server answers every frame with ``{"type": "ack", ...}``; the browser
keeps at most ``max_in_flight`` frames unacknowledged, so a slow link or backend slows
the uploads down instead of queueing them. The inbox itself keeps only the newest few
frames (``INPUT_FRAMES_MAX``), so memory stays bounded even if the worker stalls. At most
once a second the ack carries the worker's measured runtime state (``backend``).

Close codes: 4401 unauthenticated · 4403 missing permission · 4404 unknown camera ·
4400 camera is not a browser source · 4409 another browser session took over (the
superseded socket is told so on its next message, by its own handler).
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import orjson
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, _user_from_token
from app.auth.permissions import Perm
from app.core.logging import get_logger
from app.core.runtime import runtime
from app.db.models import Camera
from app.db.session import session_scope

router = APIRouter(tags=["websocket"])
log = get_logger("local_camera")

MAX_FRAME_BYTES = 2_000_000
MAX_IN_FLIGHT = 2
STATS_INTERVAL_S = 1.0
_JPEG_MAGIC = b"\xff\xd8"
# camera id -> socket currently streaming into it (one publisher per camera)
sessions: dict[str, WebSocket] = {}


def _check(token: str, camera_id: str) -> tuple[CurrentUser, dict[str, Any]]:
    with session_scope() as db:
        user = _user_from_token(db, token)
        if not user.has(Perm.CAMERAS_CONTROL):
            raise HTTPException(403, "missing permission: cameras:control")
        cam = db.get(Camera, camera_id)
        if cam is None:
            raise HTTPException(404, f"camera {camera_id} not found")
        if cam.source_type != "browser":
            raise HTTPException(400, f"camera {camera_id} is a {cam.source_type} source, not a browser Local Camera")
        return user, {"id": cam.id, "name": cam.name, "enabled": cam.enabled}


def _backend_state(camera_id: str) -> dict[str, Any] | None:
    from app.api.routes.cameras import live_runtime

    rt = live_runtime().get(camera_id)
    if rt is None:
        return None
    keys = ("status", "message", "input_fps", "processing_fps", "target_processing_fps", "frames_in", "frames_processed",
            "frames_dropped", "active_tracks", "latency_ms", "inference_ms", "resolution")
    return {k: rt.get(k) for k in keys}


async def _close(ws: WebSocket, code: int, detail: str) -> None:
    try:
        await ws.send_json({"type": "error", "data": {"detail": detail, "code": code}})
        await ws.close(code=code)
    except Exception:
        pass


@router.websocket("/ws/cameras/{camera_id}/ingest")
async def ingest(ws: WebSocket, camera_id: str) -> None:
    await ws.accept()
    camera_id = camera_id.upper()
    token = ws.query_params.get("token")
    try:
        if not token:  # fallback: {"token": "..."} as the first message
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            token = str((first or {}).get("token") or "")
        user, cam = await run_in_threadpool(_check, token, camera_id)
    except HTTPException as exc:
        await _close(ws, {401: 4401, 403: 4403, 404: 4404}.get(exc.status_code, 4400), str(exc.detail))
        return
    except (asyncio.TimeoutError, ValueError, WebSocketDisconnect):
        await _close(ws, 4401, "authentication required")
        return
    bus = runtime.bus
    if bus is None:
        await _close(ws, 1011, "event bus unavailable")
        return

    sessions[camera_id] = ws  # a previous session for this camera is superseded
    push = bus.push_input_frame if bus.backend == "memory" else None  # in-process: no I/O, no thread hop needed
    await ws.send_json({"type": "hello", "data": {"camera_id": camera_id, "name": cam["name"], "enabled": cam["enabled"],
                                                  "max_in_flight": MAX_IN_FLIGHT, "max_frame_bytes": MAX_FRAME_BYTES}})
    received = rejected = 0
    last_stats = 0.0
    started = time.time()
    try:
        while True:
            msg = await ws.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if sessions.get(camera_id) is not ws:
                await _close(ws, 4409, "another browser session started streaming to this camera")
                break
            data = msg.get("bytes")
            if data is None:
                try:
                    body = orjson.loads(msg.get("text") or "null")
                except orjson.JSONDecodeError:
                    continue
                if isinstance(body, dict) and body.get("type") == "ping":
                    await ws.send_json({"type": "pong", "data": {"backend": await run_in_threadpool(_backend_state, camera_id)}})
                continue
            if len(data) > MAX_FRAME_BYTES or not data.startswith(_JPEG_MAGIC):
                rejected += 1
                await ws.send_json({"type": "ack", "data": {"received": received, "rejected": rejected, "accepted": False,
                                                            "reason": "frame must be a JPEG of at most 2 MB"}})
                continue
            ok = push(camera_id, data) if push else await run_in_threadpool(bus.push_input_frame, camera_id, data)
            if not ok:
                rejected += 1
            else:
                received += 1
                if received == 1:
                    log.info("local camera streaming started", extra={"camera_id": camera_id, "user": user.username,
                                                                     "status": f"{len(data)} bytes/frame"})
                elif received % 100 == 0:
                    log.info("local camera frames received: %d", received, extra={"camera_id": camera_id, "user": user.username})
            ack: dict[str, Any] = {"received": received, "rejected": rejected, "accepted": ok}
            now = time.time()
            if now - last_stats >= STATS_INTERVAL_S:
                last_stats = now
                ack["backend"] = await run_in_threadpool(_backend_state, camera_id)
            await ws.send_json({"type": "ack", "data": ack})
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("local camera ingest failed", extra={"camera_id": camera_id})
    finally:
        if sessions.get(camera_id) is ws:
            sessions.pop(camera_id, None)
        log.info("local camera stream closed after %d frames (%.0fs)", received, time.time() - started,
                 extra={"camera_id": camera_id, "user": user.username})
