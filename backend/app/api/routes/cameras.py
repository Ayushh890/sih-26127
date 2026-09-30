"""Camera management, control, health and live video.

Credentials are write-only: responses carry ``has_credentials`` and a masked username
hint, never the password or a URI with embedded credentials.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, require
from app.auth.permissions import Perm
from app.camera.health.monitor import OFFLINE
from app.camera.sources import probe_source, safe_uri
from app.core.config import get_settings
from app.core.runtime import runtime
from app.core.security import decrypt_str, encrypt_str
from app.db.models import Camera, CameraHealth, Evidence, SystemEvent, VehicleObservation
from app.db.session import get_db
from app.schemas.requests import CameraCreate, CameraUpdate, ConnectionTest, FaultInjection
from app.services import audit, retention
from app.services.health_intel import camera_health_report
from app.services.scope import apply_scope, resolve_scope, scope_info
from app.services.topology import invalidate_topology

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

RUNTIME_STALE_S = 20.0


def _hint(username: str | None) -> str | None:
    if not username:
        return None
    return username[:2] + "•" * max(3, len(username) - 2)


def live_runtime() -> dict[str, dict[str, Any]]:
    if runtime.bus is None:
        return {}
    now = time.time()
    return {k: v for k, v in runtime.bus.get_runtime().items() if now - float(v.get("updated_at") or 0) < RUNTIME_STALE_S}


def camera_dict(c: Camera, rt: dict[str, Any] | None = None) -> dict[str, Any]:
    d = {
        "id": c.id, "name": c.name, "location": c.location, "latitude": c.latitude, "longitude": c.longitude,
        "source_type": c.source_type, "source_uri": safe_uri(c.source_uri), "has_credentials": bool(c.username or c.password_encrypted),
        "username_hint": _hint(c.username), "resolution": c.resolution, "lane_count": c.lane_count, "direction": c.direction,
        "road_name": c.road_name, "zone": c.zone, "camera_type": c.camera_type, "enabled": c.enabled,
        "status": c.status, "status_message": c.status_message, "last_seen_at": c.last_seen_at.isoformat() if c.last_seen_at else None,
        "processing": c.processing or {}, "calibration": c.calibration or {}, "is_demo": c.is_demo,
        "created_at": c.created_at.isoformat() if c.created_at else None, "updated_at": c.updated_at.isoformat() if c.updated_at else None,
        "runtime": None,
    }
    if rt:
        d["status"], d["status_message"] = rt.get("status", c.status), rt.get("message", c.status_message)
        d["runtime"] = {k: rt.get(k) for k in ("input_fps", "processing_fps", "target_processing_fps", "latency_ms", "inference_ms", "stage_ms",
                                                "queue_depth", "max_queue_size", "frames_in", "frames_processed", "frames_dropped", "resolution",
                                                "reconnects", "sharpness", "brightness", "active_tracks", "faults", "uptime_s", "worker_id",
                                                "status_since", "updated_at")}
        err = rt.get("last_error")
        d["runtime"]["last_error"] = safe_uri(err) if isinstance(err, str) else err
    elif c.enabled and runtime.bus is not None and c.status not in ("ERROR",):
        d["status"] = c.status if c.status != "ONLINE" else OFFLINE  # enabled but no worker reports it
    return d


def _get(db: Session, camera_id: str) -> Camera:
    c = db.get(Camera, camera_id)
    if c is None:
        raise not_found("camera")
    return c


def _control(cmd: dict[str, Any]) -> None:
    if runtime.bus is not None:
        runtime.bus.publish_control(cmd)


@router.get("", summary="List cameras with live runtime state")
def list_cameras(scope: str | None = Query(None, pattern="^(live|demo|all)$"), q: str | None = Query(None, max_length=64),
                 _: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    stmt = apply_scope(select(Camera), Camera.is_demo, sc).order_by(Camera.id)
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Camera.id.ilike(like), Camera.name.ilike(like), Camera.road_name.ilike(like), Camera.zone.ilike(like)))
    rt = live_runtime()
    return {**scope_info(sc), "cameras": [camera_dict(c, rt.get(c.id)) for c in db.scalars(stmt)]}


@router.get("/runtime", summary="Live processing metrics for all running cameras")
def all_runtime(_: CurrentUser = Depends(require(Perm.CAMERAS_READ))) -> dict[str, Any]:
    return {"cameras": live_runtime(), "ts": time.time()}


@router.post("/test-connection", summary="Open a source, grab one frame and report the result")
async def test_connection(body: ConnectionTest, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE)),
                          db: Session = Depends(get_db)) -> dict[str, Any]:
    username, password = body.username, body.password
    if body.camera_id and not password:
        cam = db.get(Camera, body.camera_id)
        if cam is not None:
            username, password = username or cam.username, decrypt_str(cam.password_encrypted)
    if body.source_type == "file":
        _check_file(body.source_uri)
    res = await run_in_threadpool(probe_source, body.source_type, body.source_uri, username=username, password=password, timeout=body.timeout_s)
    if isinstance(res.get("error"), str):
        res["error"] = safe_uri(res["error"])
    audit.record(db, user.as_dict(), "camera.test_connection", "camera", body.camera_id, {"source_type": body.source_type,
                 "uri": safe_uri(body.source_uri), "ok": res.get("ok")}, client_ip(request))
    return res


def _check_file(path: str) -> None:
    from pathlib import Path

    p = Path(path)
    if not p.is_absolute():
        p = get_settings().DATA_DIR / p
    try:
        p = p.resolve()
    except OSError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid file path") from None
    if not p.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"video file not found: {path}")


@router.post("", status_code=201, summary="Register a camera")
def create_camera(body: CameraCreate, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.get(Camera, body.id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"camera {body.id} already exists")
    if body.source_type == "demo":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "demo cameras are created by the demo seeder; use rtsp, http, webcam or file")
    if body.source_type == "file":
        _check_file(body.source_uri)
    proc = body.processing.model_dump(exclude_none=True) if body.processing else {}
    proc.setdefault("processing_fps", get_settings().DEFAULT_PROCESSING_FPS)
    proc.setdefault("confidence_threshold", get_settings().DEFAULT_CONFIDENCE_THRESHOLD)
    proc.setdefault("max_queue_size", get_settings().DEFAULT_MAX_QUEUE_SIZE)
    c = Camera(id=body.id, name=body.name, location=body.location or "", latitude=body.latitude, longitude=body.longitude,
               source_type=body.source_type, source_uri=body.source_uri, username=body.username or None,
               password_encrypted=encrypt_str(body.password) if body.password else None, resolution=body.resolution,
               lane_count=body.lane_count or 2, direction=body.direction or "N", road_name=body.road_name or "", zone=body.zone or "",
               camera_type=body.camera_type or "ANPR", enabled=bool(body.enabled), status=OFFLINE, processing=proc,
               calibration=body.calibration.model_dump(exclude_none=True) if body.calibration else {}, is_demo=False)
    db.add(c)
    db.commit()
    invalidate_topology()
    audit.record(db, user.as_dict(), "camera.create", "camera", c.id, {"source_type": c.source_type, "uri": safe_uri(c.source_uri),
                 "enabled": c.enabled}, client_ip(request))
    if c.enabled:
        _control({"cmd": "reload"})
    return camera_dict(c)


@router.get("/{camera_id}", summary="Camera detail")
def get_camera(camera_id: str, _: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    return camera_dict(_get(db, camera_id), live_runtime().get(camera_id))


@router.patch("/{camera_id}", summary="Update camera configuration (the worker restarts with the new configuration)")
def update_camera(camera_id: str, body: CameraUpdate, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    data = body.model_dump(exclude_unset=True, exclude={"password", "clear_credentials", "processing", "calibration"})
    if c.is_demo and ({"source_type", "source_uri"} & data.keys()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "the source of a demo camera cannot be changed")
    if data.get("source_type") == "demo":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot convert a camera into a demo camera")
    st = data.get("source_type", c.source_type)
    if "source_uri" in data or "source_type" in data:
        # re-validate URI against the (possibly new) source type
        CameraCreate(id=c.id, name=c.name, latitude=c.latitude, longitude=c.longitude, source_type=st,
                     source_uri=data.get("source_uri", c.source_uri))
        if st == "file":
            _check_file(data.get("source_uri", c.source_uri))
    changed = sorted(data.keys())
    for k, v in data.items():
        setattr(c, k, v)
    if body.clear_credentials:
        c.username, c.password_encrypted = None, None
        changed.append("credentials_cleared")
    if body.password:
        c.password_encrypted = encrypt_str(body.password)
        changed.append("password")
    if body.processing is not None:
        c.processing = {**(c.processing or {}), **body.processing.model_dump(exclude_unset=True)}
        changed.append("processing")
    if body.calibration is not None:
        c.calibration = {**(c.calibration or {}), **body.calibration.model_dump(exclude_unset=True)}
        changed.append("calibration")
    db.commit()
    invalidate_topology()
    audit.record(db, user.as_dict(), "camera.update", "camera", c.id, {"fields": changed}, client_ip(request))
    _control({"cmd": "reload"})
    return camera_dict(c, live_runtime().get(c.id))


@router.delete("/{camera_id}", summary="Delete a camera and all of its data")
def delete_camera(camera_id: str, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_WRITE)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    c.enabled = False
    db.commit()
    _control({"cmd": "stop", "camera_id": camera_id})
    ev = retention.delete_evidence(db, Evidence.camera_id == camera_id)
    obs = retention.delete_observations(db, VehicleObservation.camera_id == camera_id)
    db.delete(c)
    db.commit()
    invalidate_topology()
    audit.record(db, user.as_dict(), "camera.delete", "camera", camera_id, {"observations_deleted": obs, "evidence_deleted": ev}, client_ip(request))
    return {"ok": True, "observations_deleted": obs, "evidence_deleted": ev}


def _set_enabled(db: Session, c: Camera, enabled: bool, user: CurrentUser, request: Request) -> dict[str, Any]:
    c.enabled = enabled
    if not enabled:
        c.status, c.status_message = OFFLINE, "stopped by operator"
    db.commit()
    audit.record(db, user.as_dict(), "camera.start" if enabled else "camera.stop", "camera", c.id, {}, client_ip(request))
    _control({"cmd": "reload"} if enabled else {"cmd": "stop", "camera_id": c.id})
    return {"ok": True, "camera_id": c.id, "enabled": enabled}


@router.post("/{camera_id}/start", summary="Enable processing")
def start_camera(camera_id: str, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_enabled(db, _get(db, camera_id), True, user, request)


@router.post("/{camera_id}/stop", summary="Disable processing")
def stop_camera(camera_id: str, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    return _set_enabled(db, _get(db, camera_id), False, user, request)


@router.post("/{camera_id}/restart", summary="Restart the camera worker")
def restart_camera(camera_id: str, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    if not c.enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "camera is disabled; start it first")
    _control({"cmd": "restart", "camera_id": c.id})
    audit.record(db, user.as_dict(), "camera.restart", "camera", c.id, {}, client_ip(request))
    return {"ok": True}


@router.post("/{camera_id}/simulate-fault", summary="Fault injection for resilience testing (offline/blur/low_fps/dark)")
def simulate_fault(camera_id: str, body: FaultInjection, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)),
                   db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    if c.id not in live_runtime():
        raise HTTPException(status.HTTP_409_CONFLICT, "camera is not running on any worker")
    _control({"cmd": "fault", "camera_id": c.id, "kind": body.kind, "seconds": body.seconds})
    audit.record(db, user.as_dict(), "camera.fault_injection", "camera", c.id, {"kind": body.kind, "seconds": body.seconds}, client_ip(request))
    return {"ok": True, "camera_id": c.id, "kind": body.kind, "seconds": body.seconds,
            "note": "Fault injection affects only this camera's input; the rest of the system keeps running."}


@router.post("/{camera_id}/simulate-disconnect", summary="Simulate a stream disconnect")
def simulate_disconnect(camera_id: str, request: Request, seconds: float = Query(30, ge=1, le=600),
                        user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)), db: Session = Depends(get_db)) -> dict[str, Any]:
    return simulate_fault(camera_id, FaultInjection(kind="offline", seconds=seconds), request, user, db)


@router.post("/{camera_id}/clear-faults", summary="Clear injected faults")
def clear_faults(camera_id: str, request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    _control({"cmd": "clear_faults", "camera_id": c.id})
    audit.record(db, user.as_dict(), "camera.clear_faults", "camera", c.id, {}, client_ip(request))
    return {"ok": True}


@router.get("/{camera_id}/health", summary="Camera health intelligence: score, factors and recommendations")
def camera_health(camera_id: str, window_minutes: int = Query(60, ge=5, le=1440), _: CurrentUser = Depends(require(Perm.CAMERAS_READ)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    c = _get(db, camera_id)
    rt = live_runtime().get(c.id)
    rep = camera_health_report(db, c, rt, window_s=window_minutes * 60)
    rep["runtime"] = camera_dict(c, rt)["runtime"]
    return rep


@router.get("/{camera_id}/health/history", summary="Recorded health samples")
def health_history(camera_id: str, minutes: int = Query(60, ge=1, le=10080), _: CurrentUser = Depends(require(Perm.CAMERAS_READ)),
                   db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _get(db, camera_id)
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    rows = db.scalars(select(CameraHealth).where(CameraHealth.camera_id == camera_id, CameraHealth.ts >= since).order_by(CameraHealth.ts).limit(5000))
    return [{"ts": r.ts.isoformat(), "status": r.status, "input_fps": r.input_fps, "processing_fps": r.processing_fps, "latency_ms": r.latency_ms,
             "inference_ms": r.inference_ms, "queue_depth": r.queue_depth, "frames_processed": r.frames_processed, "frames_dropped": r.frames_dropped,
             "vehicles_detected": r.vehicles_detected, "plates_read": r.plates_read, "avg_ocr_confidence": r.avg_ocr_confidence,
             "blur_score": r.blur_score, "brightness": r.brightness, "reconnects": r.reconnects,
             "error": safe_uri(r.error) if r.error else None} for r in rows]


@router.get("/{camera_id}/logs", summary="Camera status transitions and errors")
def camera_logs(camera_id: str, limit: int = Query(100, ge=1, le=1000), _: CurrentUser = Depends(require(Perm.CAMERAS_READ)),
                db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    _get(db, camera_id)
    rows = db.scalars(select(SystemEvent).where(SystemEvent.camera_id == camera_id).order_by(SystemEvent.ts.desc()).limit(limit))
    return [{"id": r.id, "ts": r.ts.isoformat(), "level": r.level, "source": r.source, "event_type": r.event_type,
             "message": safe_uri(r.message), "details": r.details} for r in rows]


def _frame(camera_id: str, overlay: bool) -> bytes | None:
    if runtime.bus is None:
        return None
    got = runtime.bus.get_frame(camera_id, "annotated" if overlay else "raw")
    if got is None and overlay:
        got = runtime.bus.get_frame(camera_id, "raw")
    return got[0] if got else None


@router.get("/{camera_id}/snapshot.jpg", summary="Latest frame (JPEG)", response_class=Response)
def snapshot(camera_id: str, overlay: bool = True, _: CurrentUser = Depends(require(Perm.STREAM_VIEW)), db: Session = Depends(get_db)) -> Response:
    _get(db, camera_id)
    jpg = _frame(camera_id, overlay)
    if jpg is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "no recent frame: the camera is not being processed or is offline")
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@router.get("/{camera_id}/stream.mjpg", summary="Live MJPEG preview (raw or with detection overlay)")
async def stream(camera_id: str, request: Request, overlay: bool = True, fps: float = Query(6.0, ge=0.5, le=15),
                 _: CurrentUser = Depends(require(Perm.STREAM_VIEW)), db: Session = Depends(get_db)) -> StreamingResponse:
    _get(db, camera_id)
    db.close()  # do not hold a DB connection for the lifetime of the stream
    boundary = "nirnayframe"

    async def gen():  # type: ignore[no-untyped-def]
        last = None
        idle = 0.0
        while not await request.is_disconnected():
            jpg = _frame(camera_id, overlay)
            if jpg is not None and jpg is not last:
                last, idle = jpg, 0.0
                yield (f"--{boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: {len(jpg)}\r\n\r\n").encode() + jpg + b"\r\n"
            else:
                idle += 1.0 / fps
                if idle > 60:  # no frames for a minute: end the stream, the client reconnects
                    break
            await asyncio.sleep(1.0 / fps)

    return StreamingResponse(gen(), media_type=f"multipart/x-mixed-replace; boundary={boundary}", headers={"Cache-Control": "no-store"})
