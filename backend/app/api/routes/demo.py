"""Demo scenario control and the emergency-corridor simulator."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, protect, require
from app.auth.permissions import Perm
from app.core.runtime import runtime
from app.db.session import get_db
from app.schemas.requests import CorridorRequest
from app.services import audit, corridor, demo_control
from app.services.settings_service import settings_cache
from app.services.topology import get_topology

router = APIRouter(prefix="/api", tags=["demo", "corridor"])


@router.get("/demo/timeline", summary="Scheduled scripted events of the synthetic demo scenario")
def timeline(user: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    if not demo_control.demo_cameras(db):
        return {"synthetic": True, "enabled": False, "events": [], "notice": "Demo cameras are not configured (SEED_DEMO=false)."}
    return protect(db, user, {"enabled": True, **demo_control.timeline(db)})


@router.post("/demo/restart", summary="Purge demo data and restart the demo scenario from t=0 (live data untouched)")
def restart(request: Request, user: CurrentUser = Depends(require(Perm.CAMERAS_CONTROL)), db: Session = Depends(get_db)) -> dict[str, Any]:
    cams = demo_control.demo_cameras(db)
    if not cams:
        raise HTTPException(status.HTTP_409_CONFLICT, "no demo cameras configured")
    out = demo_control.restart_timeline(db)
    if runtime.bus is not None:
        for c in cams:  # workers re-read the new scenario clock on restart
            runtime.bus.publish_control({"cmd": "restart", "camera_id": c.id})
    audit.record(db, user.as_dict(), "demo.restart", "demo", None, {"purged": out["purged"]}, client_ip(request))
    return out


@router.post("/corridor/plan", summary="Emergency corridor plan — SIMULATION ONLY, never controls signals")
def plan(body: CorridorRequest, request: Request, user: CurrentUser = Depends(require(Perm.CORRIDOR_USE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        out = corridor.plan(db, get_topology(db), settings_cache.section(db, "congestion"), body.origin, body.destination, body.depart,
                            body.priority_speedup)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(db, user.as_dict(), "corridor.plan", "corridor", f"{body.origin}->{body.destination}",
                 {"estimated_s": out["estimated_s"]}, client_ip(request))
    return out
