"""Watchlist management. Nothing is pre-listed: entries exist only when an authorised
user adds them, each with a mandatory reason, and every change is audited."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, protect, require
from app.auth.permissions import Perm
from app.db.models import Camera, GlobalVehicle, WatchlistEntry
from app.db.session import get_db
from app.schemas.requests import WatchlistCreate, WatchlistUpdate
from app.services import audit
from app.services.settings_service import settings_cache
from app.services.views import observation_dict
from app.services.watchlist import entry_dict, retrospective

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.get("", summary="List watchlist entries")
def list_entries(active: bool | None = None, user: CurrentUser = Depends(require(Perm.WATCHLIST_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(WatchlistEntry)
    if active is not None:
        q = q.where(WatchlistEntry.active.is_(active))
    rows = db.scalars(q.order_by(WatchlistEntry.created_at.desc()))
    return protect(db, user, {"results": [entry_dict(e) for e in rows]})


@router.post("", status_code=status.HTTP_201_CREATED, summary="Add a plate to the watchlist (reason required, audited)")
def create(body: WatchlistCreate, request: Request, user: CurrentUser = Depends(require(Perm.WATCHLIST_WRITE)),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.scalar(select(WatchlistEntry).where(WatchlistEntry.plate == body.plate, WatchlistEntry.active.is_(True))):
        raise HTTPException(status.HTTP_409_CONFLICT, "an active entry for this plate already exists")
    e = WatchlistEntry(plate=body.plate, reason=body.reason, description=body.description, priority=body.priority, match_mode=body.match_mode,
                       expires_at=body.expires_at, active=True, created_by=user.username)
    db.add(e)
    db.flush()
    audit.record(db, user.as_dict(), "watchlist.create", "watchlist", str(e.id),
                 {"plate": e.plate, "reason": e.reason, "priority": e.priority, "match_mode": e.match_mode}, client_ip(request))
    return entry_dict(e)


@router.patch("/{entry_id}", summary="Update a watchlist entry (audited)")
def update(entry_id: int, body: WatchlistUpdate, request: Request, user: CurrentUser = Depends(require(Perm.WATCHLIST_WRITE)),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    e = db.get(WatchlistEntry, entry_id)
    if e is None:
        raise not_found("watchlist entry")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(e, k, v)
    e.updated_at = datetime.now(timezone.utc)
    audit.record(db, user.as_dict(), "watchlist.update", "watchlist", str(e.id), {"plate": e.plate, "changes": changes}, client_ip(request))
    return entry_dict(e)


@router.delete("/{entry_id}", summary="Remove a watchlist entry (audited)")
def delete(entry_id: int, request: Request, user: CurrentUser = Depends(require(Perm.WATCHLIST_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    e = db.get(WatchlistEntry, entry_id)
    if e is None:
        raise not_found("watchlist entry")
    plate = e.plate
    db.delete(e)
    audit.record(db, user.as_dict(), "watchlist.delete", "watchlist", str(entry_id), {"plate": plate}, client_ip(request))
    return {"deleted": entry_id}


@router.get("/{entry_id}/retrospective", summary="Search past sightings for a watchlist entry (audited)")
def retro(entry_id: int, request: Request, days: int = Query(7, ge=1, le=90), user: CurrentUser = Depends(require(Perm.WATCHLIST_READ, Perm.VEHICLES_SEARCH)),
          db: Session = Depends(get_db)) -> dict[str, Any]:
    e = db.get(WatchlistEntry, entry_id)
    if e is None:
        raise not_found("watchlist entry")
    fuzzy_max = float(settings_cache.section(db, "alerts")["WATCHLIST_MATCH"]["fuzzy_max_distance"])
    hits = retrospective(db, e, days, fuzzy_max)
    names = dict(db.execute(select(Camera.id, Camera.name)).all())
    codes = dict(db.execute(select(GlobalVehicle.id, GlobalVehicle.code).where(
        GlobalVehicle.id.in_({o.global_vehicle_id for o, _ in hits if o.global_vehicle_id}))).all())
    results = [{**observation_dict(o, codes.get(o.global_vehicle_id or -1), names.get(o.camera_id)), "plate_distance": d} for o, d in hits]
    audit.record(db, user.as_dict(), "watchlist.retrospective", "watchlist", str(e.id), {"plate": e.plate, "days": days, "results": len(results)},
                 client_ip(request))
    return protect(db, user, {"entry": entry_dict(e), "days": days, "results": results})
