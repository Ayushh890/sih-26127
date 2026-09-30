"""Aggregate traffic analytics. All numbers are computed from persisted data; an empty
period returns ``has_data: false`` with the message "No data available for this period."

Aggregates never list individual vehicles. The OD drill-down (which does) requires the
``analytics:od_individual`` permission and is audited.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, protect, require
from app.auth.permissions import Perm
from app.db.models import Camera, GlobalVehicle
from app.db.session import get_db
from app.schemas.requests import IncidentQuery
from app.services import analytics, audit
from app.services.scheduler import analytics_snapshot
from app.services.scope import resolve_scope, scope_camera_ids, scope_info
from app.services.settings_service import settings_cache
from app.services.topology import get_topology

router = APIRouter(prefix="/api/analytics", tags=["analytics"])

NO_DATA = "No data available for this period."
PERIODS = {"15m": 900, "1h": 3600, "6h": 6 * 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}
SCOPE_Q = Query(None, pattern="^(live|demo|all)$")
PERIOD_Q = Query("1h", pattern="^(15m|1h|6h|24h|7d|30d)$")
UNTIL_Q = Query(None, description="Evaluate as of this time instead of now (historical view)")


def _asof(until: datetime | None) -> datetime:
    now = datetime.now(timezone.utc)
    if until is None:
        return now
    until = until if until.tzinfo else until.replace(tzinfo=timezone.utc)
    return min(until, now)


def _window(period: str, since: datetime | None, until: datetime | None) -> tuple[datetime, datetime]:
    until = until or datetime.now(timezone.utc)
    since = since or until - timedelta(seconds=PERIODS[period])
    if since.tzinfo is None:
        since = since.replace(tzinfo=timezone.utc)
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    if since >= until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "since must be before until")
    if until - since > timedelta(days=92):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "period longer than 92 days")
    return since, until


def _cams(db: Session, sc: str, camera_id: str | None) -> list[str]:
    ids = scope_camera_ids(db, sc)
    if camera_id:
        if camera_id not in ids:
            raise not_found("camera in this scope")
        return [camera_id]
    return ids


def _envelope(sc: str, since: datetime, until: datetime, has_data: bool, **body: Any) -> dict[str, Any]:
    return {**scope_info(sc), "since": since.isoformat(), "until": until.isoformat(), "has_data": has_data,
            "message": None if has_data else NO_DATA, **body}


@router.get("/overview", summary="Network snapshot (same payload as the analytics_updated WebSocket event)")
def overview(scope: str | None = SCOPE_Q, user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)),
             db: Session = Depends(get_db)) -> dict[str, Any]:
    snap = analytics_snapshot(db)
    if scope is None:
        return snap
    sc = resolve_scope(db, scope)
    ids = set(scope_camera_ids(db, sc))
    return {**snap, **scope_info(sc), "cameras": [c for c in snap["cameras"] if c["camera_id"] in ids],
            "scopes": {k: v for k, v in snap["scopes"].items() if sc == "all" or k == sc}}


@router.get("/summary", summary="Counts, speeds, occupancy, queue, density and class mix for a period")
def summary(period: str = PERIOD_Q, since: datetime | None = None, until: datetime | None = None, camera_id: str | None = Query(None, max_length=32),
            scope: str | None = SCOPE_Q, user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    since, until = _window(period, since, until)
    s = analytics.summary(db, _cams(db, sc, camera_id), since, until)
    return _envelope(sc, since, until, s["totals"] is not None or s["observations"] > 0, **s)


@router.get("/timeseries", summary="Bucketed traffic metrics")
def timeseries(period: str = PERIOD_Q, since: datetime | None = None, until: datetime | None = None, bucket_s: int = Query(0, ge=0, le=86400),
               camera_id: str | None = Query(None, max_length=32), scope: str | None = SCOPE_Q,
               user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    since, until = _window(period, since, until)
    span = (until - since).total_seconds()
    bucket = bucket_s or (60 if span <= 3600 else 300 if span <= 6 * 3600 else 900 if span <= 86400 else 3600 if span <= 7 * 86400 else 21600)
    bucket = max(bucket, 10)
    rows = analytics.time_series(db, _cams(db, sc, camera_id), since, until, bucket)
    return _envelope(sc, since, until, bool(rows), bucket_s=bucket, series=rows)


@router.get("/congestion", summary="Explainable congestion score per camera (current window)")
def congestion(camera_id: str | None = Query(None, max_length=32), scope: str | None = SCOPE_Q, until: datetime | None = UNTIL_Q,
               user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    cfg = settings_cache.section(db, "congestion")
    now = _asof(until)
    rows = analytics.network_congestion(db, cfg, get_topology(db), _cams(db, sc, camera_id), now)
    scored = [r for r in rows if r["score"] is not None]
    return {**_envelope(sc, now - timedelta(seconds=cfg["window_s"]), now, bool(scored), cameras=rows),
            "thresholds": cfg["thresholds"], "weights": cfg["weights"], "levels": [*analytics.LEVELS, "NO_DATA"]}


@router.get("/hotspots", summary="Cameras ranked by period congestion and alert load")
def hotspots(period: str = PERIOD_Q, since: datetime | None = None, until: datetime | None = None, scope: str | None = SCOPE_Q,
             user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    since, until = _window(period, since, until)
    rows = analytics.hotspots(db, settings_cache.section(db, "congestion"), get_topology(db), _cams(db, sc, None), since, until)
    return _envelope(sc, since, until, bool(rows), hotspots=rows)


@router.get("/od-matrix", summary="Origin–destination journey counts (small cells suppressed)")
def od_matrix(period: str = PERIOD_Q, since: datetime | None = None, until: datetime | None = None, scope: str | None = SCOPE_Q,
              user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    since, until = _window(period, since, until)
    min_count = int(settings_cache.section(db, "privacy").get("od_min_count", 1))
    # users without drill-down rights never see cells small enough to single out a vehicle
    if not user.has(Perm.OD_INDIVIDUAL):
        min_count = max(min_count, 3)
    m = analytics.od_matrix(db, _cams(db, sc, None), since, until, min_count)
    names = dict(db.execute(select(Camera.id, Camera.name)).all())
    return _envelope(sc, since, until, m["total_journeys"] > 0, **m, camera_names={c: names.get(c) for c in m["cameras"]},
                     drilldown_allowed=user.has(Perm.OD_INDIVIDUAL))


@router.get("/od-matrix/vehicles", summary="Journeys behind one OD cell (authorised users only, audited)")
def od_vehicles(request: Request, origin: str = Query(max_length=32), destination: str = Query(max_length=32), period: str = PERIOD_Q,
                since: datetime | None = None, until: datetime | None = None, scope: str | None = SCOPE_Q,
                user: CurrentUser = Depends(require(Perm.ANALYTICS_READ, Perm.OD_INDIVIDUAL)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    since, until = _window(period, since, until)
    rows = analytics.od_cell_vehicles(db, _cams(db, sc, None), since, until, origin, destination)
    vs = {v.id: v for v in db.scalars(select(GlobalVehicle).where(GlobalVehicle.id.in_({r["global_vehicle_id"] for r in rows})))}
    for r in rows:
        v = vs.get(r["global_vehicle_id"])
        r.update({"vehicle_code": v.code if v else None, "plate_text": v.plate_text if v else None})
    audit.record(db, user.as_dict(), "analytics.od_drilldown", "od_cell", f"{origin}->{destination}",
                 {"since": since.isoformat(), "until": until.isoformat(), "results": len(rows)}, client_ip(request))
    return protect(db, user, _envelope(sc, since, until, bool(rows), origin=origin, destination=destination, journeys=rows))


@router.get("/travel-times", summary="Segment travel times vs baseline (travel-time anomaly)")
def travel_times(window_s: int = Query(900, ge=60, le=86400), scope: str | None = SCOPE_Q, until: datetime | None = UNTIL_Q,
                 user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    rule = settings_cache.section(db, "alerts")["TRAVEL_TIME_ANOMALY"]
    ccfg = settings_cache.section(db, "congestion")
    now = _asof(until)
    rows = analytics.travel_time_anomalies(db, get_topology(db), _cams(db, sc, None), window_s, float(rule["ratio"]), 1,
                                           ccfg["baseline_s"], now)
    return _envelope(sc, now - timedelta(seconds=window_s), now, bool(rows), anomaly_ratio=rule["ratio"],
                     min_samples_for_alert=rule["min_samples"], segments=rows)


@router.post("/incident-impact", summary="Before/during comparison for an incident at a camera")
def incident_impact(body: IncidentQuery, user: CurrentUser = Depends(require(Perm.ANALYTICS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    cam = db.get(Camera, body.camera_id)
    if cam is None:
        raise not_found("camera")
    end = body.end or datetime.now(timezone.utc)
    start = body.start if body.start.tzinfo else body.start.replace(tzinfo=timezone.utc)
    end = end if end.tzinfo else end.replace(tzinfo=timezone.utc)
    try:
        r = analytics.incident_impact(db, settings_cache.section(db, "congestion"), get_topology(db), body.camera_id, start, end)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    has = r["site"]["during"] is not None
    return {**r, **scope_info("demo" if cam.is_demo else "live"), "has_data": has, "message": None if has else NO_DATA}


# Aliases under the endpoint names used in the SIH26127 specification; same handlers and payloads.
for _path, _handler, _summary in (("/traffic-volume", timeseries, "Alias of /timeseries: bucketed traffic volume"),
                                   ("/od", od_matrix, "Alias of /od-matrix"),
                                   ("/travel-time", travel_times, "Alias of /travel-times")):
    router.add_api_route(_path, _handler, methods=["GET"], summary=_summary)
