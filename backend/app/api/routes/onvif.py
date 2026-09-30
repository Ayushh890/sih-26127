"""ONVIF camera discovery (WS-Discovery) and device probing (profiles + RTSP URIs)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from starlette.concurrency import run_in_threadpool

from app.api.deps import CurrentUser, client_ip, require
from app.auth.permissions import Perm
from app.camera.onvif import OnvifClient, OnvifError, discover
from app.db.session import session_scope
from app.schemas.requests import OnvifDiscover, OnvifProbe
from app.services import audit

router = APIRouter(prefix="/api/onvif", tags=["onvif"])


@router.post("/discover", summary="Find ONVIF cameras on the local network segment")
async def onvif_discover(request: Request, body: OnvifDiscover | None = None, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE))) -> dict[str, Any]:
    result = await run_in_threadpool(discover, body.timeout_s if body else 3.0)
    with session_scope() as db:
        audit.record(db, user.as_dict(), "camera.onvif_discover", "onvif", None, {"found": len(result["devices"])}, client_ip(request), commit=False)
    return result


@router.post("/probe", summary="Read device info, media profiles and stream URIs from an ONVIF device")
async def onvif_probe(request: Request, req: OnvifProbe, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE))) -> dict[str, Any]:
    client = OnvifClient(req.xaddr, req.username, req.password, req.timeout_s)
    try:
        result = await run_in_threadpool(client.probe)
    except OnvifError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    with session_scope() as db:
        audit.record(db, user.as_dict(), "camera.onvif_probe", "onvif", req.xaddr, {"profiles": len(result["profiles"])}, client_ip(request), commit=False)
    return result
