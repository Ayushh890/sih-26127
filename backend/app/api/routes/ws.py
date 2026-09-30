"""WebSocket event stream ``/ws/events?token=…``.

Event types: vehicle_detected, plate_read, vehicle_matched, trajectory_updated,
alert_created, alert_updated, camera_status_changed, analytics_updated.

Each connection is authorised with the user's JWT (browsers cannot set headers on a
WebSocket, so the token travels as a query parameter or as the first message). Events
are filtered by permission and plate text is pseudonymised for roles without raw access.
The account is re-checked periodically so disabled users are disconnected.
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
from app.db.session import session_scope
from app.services import privacy
from app.services.settings_service import settings_cache

router = APIRouter(tags=["websocket"])
log = get_logger("ws")

EVENT_PERMS: dict[str, Perm] = {
    "vehicle_detected": Perm.CAMERAS_READ,
    "plate_read": Perm.TRAJECTORY_READ,
    "vehicle_matched": Perm.TRAJECTORY_READ,
    "trajectory_updated": Perm.TRAJECTORY_READ,
    "alert_created": Perm.ALERTS_READ,
    "alert_updated": Perm.ALERTS_READ,
    "camera_status_changed": Perm.CAMERAS_READ,
    "analytics_updated": Perm.ANALYTICS_READ,
}
RECHECK_S = 60.0
connections = {"open": 0, "total": 0}


def _auth(token: str) -> tuple[CurrentUser, bool]:
    with session_scope() as db:
        user = _user_from_token(db, token)
        raw = privacy.can_view_raw(user.role, settings_cache.section(db, "privacy"))
    return user, raw


def filter_event(event: dict[str, Any], user: CurrentUser, raw_ok: bool, types: set[str] | None) -> dict[str, Any] | None:
    """Return the event as this user may see it, or ``None`` to drop it."""
    typ = event.get("type")
    perm = EVENT_PERMS.get(str(typ))
    if perm is None or not user.has(perm) or (types and typ not in types):
        return None
    data = dict(event.get("data") or {})
    data.pop("sensitive", None)
    out = {**event, "data": data}
    return out if raw_ok else privacy.mask(out)


@router.websocket("/ws/events")
async def events(ws: WebSocket) -> None:
    await ws.accept()
    token = ws.query_params.get("token")
    try:
        if not token:  # fallback: {"token": "..."} as the first message
            first = await asyncio.wait_for(ws.receive_json(), timeout=10)
            token = str((first or {}).get("token") or "")
        user, raw_ok = await run_in_threadpool(_auth, token)
    except (HTTPException, asyncio.TimeoutError, ValueError, WebSocketDisconnect) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else "authentication required"
        try:
            await ws.send_json({"type": "error", "data": {"detail": detail}})
            await ws.close(code=4401)
        except Exception:
            pass
        return
    bus = runtime.bus
    if bus is None:
        await ws.send_json({"type": "error", "data": {"detail": "event bus unavailable"}})
        await ws.close(code=1011)
        return
    types_q = ws.query_params.get("types")
    types = {t.strip() for t in types_q.split(",") if t.strip()} if types_q else None
    connections["open"] += 1
    connections["total"] += 1
    await ws.send_json({"type": "hello", "data": {"user": user.username, "role": user.role, "raw_plates": raw_ok,
                                                  "events": sorted(t for t, p in EVENT_PERMS.items() if user.has(p))}})

    async def reader() -> None:  # keeps the socket alive and answers pings; the client may change the type filter
        nonlocal types
        while True:
            msg = await ws.receive_text()
            try:
                body = orjson.loads(msg)
            except orjson.JSONDecodeError:
                continue
            if body.get("type") == "ping":
                await ws.send_json({"type": "pong", "ts": time.time()})
            elif body.get("type") == "subscribe":
                types = {str(t) for t in body.get("types") or []} or None

    reader_task = asyncio.create_task(reader())
    last_check = time.time()
    try:
        async for ev in bus.ui_listener():
            if reader_task.done():
                break
            if time.time() - last_check > RECHECK_S:
                last_check = time.time()
                try:
                    user, raw_ok = await run_in_threadpool(_auth, token)
                except HTTPException:
                    await ws.close(code=4401)
                    break
            out = filter_event(ev, user, raw_ok, types)
            if out is not None:
                await ws.send_text(orjson.dumps(out, option=orjson.OPT_SERIALIZE_NUMPY, default=str).decode())
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception:
        log.exception("websocket stream failed")
    finally:
        reader_task.cancel()
        connections["open"] -= 1
