"""Vehicle search, sightings, global vehicle identities and trajectories.

Every search is written to the audit log (search audit history). Users without
``plates:view_raw`` receive pseudonymised plates and may search by pseudonym only.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, can_view_raw, client_ip, not_found, protect, require
from app.auth.permissions import Perm
from app.db.models import Camera, Evidence, GlobalVehicle, PlateRead, TrajectoryPoint, VehicleObservation, VehicleTrack
from app.db.session import get_db
from app.ml.ocr.normalize import clean, normalize_plate, weighted_edit_distance
from app.services import audit
from app.services.privacy import pseudonym_prefix
from app.services.scope import apply_scope, resolve_scope, scope_info
from app.services.topology import get_topology
from app.services.views import evidence_dict, iso, observation_dict, point_dict, vehicle_dict

router = APIRouter(prefix="/api", tags=["vehicles"])

SCOPE_Q = Query(None, pattern="^(live|demo|all)$")

def _names(db: Session) -> dict[str, str]:
    return dict(db.execute(select(Camera.id, Camera.name)).all())

def _codes(db: Session, ids: set[int]) -> dict[int, str]:
    if not ids:
        return {}
    return dict(db.execute(select(GlobalVehicle.id, GlobalVehicle.code).where(GlobalVehicle.id.in_(ids))).all())

def _obs_list(db: Session, rows: list[VehicleObservation]) -> list[dict[str, Any]]:
    names = _names(db)
    codes = _codes(db, {o.global_vehicle_id for o in rows if o.global_vehicle_id})
    return [observation_dict(o, codes.get(o.global_vehicle_id or -1), names.get(o.camera_id)) for o in rows]

@router.get("/vehicles/search", summary="Search sightings by plate (exact, partial, fuzzy or pseudonym) and filters")
def search(request: Request, plate: str | None = Query(None, max_length=24), fuzzy: bool = False,
           max_distance: float = Query(1.5, ge=0, le=4), camera_id: str | None = Query(None, max_length=32),
           since: datetime | None = None, until: datetime | None = None, vehicle_class: str | None = Query(None, max_length=16),
           color: str | None = Query(None, max_length=16), min_plate_confidence: float | None = Query(None, ge=0, le=1),
           match_level: str | None = Query(None, pattern="^(HIGH|MEDIUM|LOW|NEW)$"), unreadable_plate: bool | None = None,
           scope: str | None = SCOPE_Q, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0, le=100000),
           user: CurrentUser = Depends(require(Perm.VEHICLES_SEARCH)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    raw_ok = can_view_raw(db, user)
    until = until or datetime.now(timezone.utc)
    since = since or until - timedelta(days=7)
    if since > until:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "since must be before until")
    q = select(VehicleObservation).where(VehicleObservation.observed_at >= since, VehicleObservation.observed_at <= until)
    q = apply_scope(q, VehicleObservation.is_demo, sc)
    mode = None
    fuzzy_hits: dict[str, float] = {}
    if plate:
        prefix = pseudonym_prefix(plate)
        if prefix:
            mode = "pseudonym"
            q = q.where(VehicleObservation.plate_hash.like(prefix + "%"))
        else:
            if not raw_ok:
                raise HTTPException(status.HTTP_403_FORBIDDEN, "privacy mode: your role may only search by pseudonym (PSN-…)")
            text = clean(plate)
            if len(text) < 2:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "plate query needs at least 2 letters/digits")
            if fuzzy:
                mode = "fuzzy"
                target = normalize_plate(text).normalized or text
                cands = db.scalars(select(VehicleObservation.plate_text).distinct().where(
                    VehicleObservation.plate_text.is_not(None), VehicleObservation.observed_at >= since, VehicleObservation.observed_at <= until,
                    func.length(VehicleObservation.plate_text).between(len(target) - 2, len(target) + 2)).limit(20000))
                for c in cands:
                    d = weighted_edit_distance(target, c)
                    if d <= max_distance:
                        fuzzy_hits[c] = round(d, 2)
                q = q.where(VehicleObservation.plate_text.in_(list(fuzzy_hits) or [""]))
            else:
                mode = "partial"
                q = q.where(or_(VehicleObservation.plate_text.like(f"%{text}%"), VehicleObservation.plate_raw.like(f"%{text}%")))
    if camera_id:
        q = q.where(VehicleObservation.camera_id == camera_id)
    if vehicle_class:
        q = q.where(VehicleObservation.vehicle_class == vehicle_class)
    if color:
        q = q.where(VehicleObservation.vehicle_color == color)
    if min_plate_confidence is not None:
        q = q.where(VehicleObservation.plate_confidence >= min_plate_confidence)
    if match_level:
        q = q.where(VehicleObservation.match_confidence_level == match_level)
    if unreadable_plate is True:
        q = q.where(VehicleObservation.plate_text.is_(None))
    elif unreadable_plate is False:
        q = q.where(VehicleObservation.plate_text.is_not(None))
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = list(db.scalars(q.order_by(VehicleObservation.observed_at.desc()).limit(limit).offset(offset)))
    results = _obs_list(db, rows)
    for r in results:
        if r["plate_text"] in fuzzy_hits:
            r["plate_distance"] = fuzzy_hits[r["plate_text"]]
    audit.record(db, user.as_dict(), "vehicle.search", "observation", None,
                 {"plate_query": plate, "mode": mode, "camera_id": camera_id, "since": iso(since), "until": iso(until), "scope": sc,
                  "vehicle_class": vehicle_class, "color": color, "results": total}, client_ip(request))
    return protect(db, user, {**scope_info(sc), "total": total, "limit": limit, "offset": offset, "mode": mode,
                              "since": iso(since), "until": iso(until), "results": results})

@router.get("/observations", summary="Recent sightings (live feed)")
def recent(camera_id: str | None = Query(None, max_length=32), since_id: int | None = Query(None, ge=0), scope: str | None = SCOPE_Q,
           limit: int = Query(50, ge=1, le=200), user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)),
           db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    q = apply_scope(select(VehicleObservation), VehicleObservation.is_demo, sc)
    if camera_id:
        q = q.where(VehicleObservation.camera_id == camera_id)
    if since_id:
        q = q.where(VehicleObservation.id > since_id)
    rows = list(db.scalars(q.order_by(VehicleObservation.id.desc()).limit(limit)))
    return protect(db, user, {**scope_info(sc), "results": _obs_list(db, rows)})

@router.get("/observations/{obs_id}", summary="Sighting detail: plate reads, identity match breakdown, track, evidence")
def observation_detail(obs_id: int, request: Request, user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)),
                       db: Session = Depends(get_db)) -> dict[str, Any]:
    o = db.get(VehicleObservation, obs_id)
    if o is None:
        raise not_found("observation")
    d = _obs_list(db, [o])[0]
    d["match_components"] = o.match_components or {}
    d["color_rgb"] = o.color_rgb
    d["bbox"] = o.bbox
    d["plate_reads"] = [{"ts": iso(r.ts), "raw_text": r.raw_text, "normalized_text": r.normalized_text, "confidence": r.confidence,
                         "char_confidences": r.char_confidences, "is_valid_format": r.is_valid_format, "corrections": r.corrections,
                         "plate_bbox": r.plate_bbox}
                        for r in db.scalars(select(PlateRead).where(PlateRead.observation_id == o.id).order_by(PlateRead.ts))]
    tr = db.get(VehicleTrack, o.track_id) if o.track_id else None
    d["track"] = {"frames": tr.frames, "started_at": iso(tr.started_at), "ended_at": iso(tr.ended_at), "path": tr.path} if tr else None
    prev = db.get(VehicleObservation, o.previous_observation_id) if o.previous_observation_id else None
    d["previous"] = _obs_list(db, [prev])[0] if prev else None
    ev = db.get(Evidence, o.evidence_id) if o.evidence_id and user.has(Perm.EVIDENCE_READ) else None
    d["evidence"] = evidence_dict(ev) if ev else None
    d["evidence_access"] = user.has(Perm.EVIDENCE_READ)
    if can_view_raw(db, user):
        audit.record(db, user.as_dict(), "observation.view", "observation", str(o.id), {"plate": o.plate_text}, client_ip(request))
    return protect(db, user, d)

def _vehicle(db: Session, ref: str) -> GlobalVehicle:
    v = db.scalar(select(GlobalVehicle).where(GlobalVehicle.code == ref.upper())) if not ref.isdigit() else db.get(GlobalVehicle, int(ref))
    if v is None:
        raise not_found("vehicle")
    return v

@router.get("/vehicles", summary="Global vehicle identities (most recently seen first)")
def list_vehicles(min_cameras: int = Query(1, ge=1, le=50), since: datetime | None = None, scope: str | None = SCOPE_Q,
                  limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0), user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    q = apply_scope(select(GlobalVehicle), GlobalVehicle.is_demo, sc).where(GlobalVehicle.camera_count >= min_cameras)
    if since:
        q = q.where(GlobalVehicle.last_seen_at >= since)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(GlobalVehicle.last_seen_at.desc()).limit(limit).offset(offset))
    return protect(db, user, {**scope_info(sc), "total": total, "results": [vehicle_dict(v) for v in rows]})

@router.get("/vehicles/{ref}", summary="Global vehicle identity (by id or VEH-code)")
def vehicle_detail(ref: str, user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    v = _vehicle(db, ref)
    rows = list(db.scalars(select(VehicleObservation).where(VehicleObservation.global_vehicle_id == v.id).order_by(VehicleObservation.observed_at)))
    d = vehicle_dict(v)
    d["observations"] = _obs_list(db, rows)
    d["cameras"] = sorted({o.camera_id for o in rows})
    return protect(db, user, d)

@router.get("/vehicles/{ref}/trajectory", summary="Confidence-annotated trajectory with road geometry for replay")
def trajectory(ref: str, request: Request, journey: int | None = Query(None, ge=0), user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)),
               db: Session = Depends(get_db)) -> dict[str, Any]:
    v = _vehicle(db, ref)
    q = select(TrajectoryPoint).where(TrajectoryPoint.global_vehicle_id == v.id)
    if journey is not None:
        q = q.where(TrajectoryPoint.journey_index == journey)
    pts = list(db.scalars(q.order_by(TrajectoryPoint.seq)))
    graph = get_topology(db)
    names = _names(db)
    obs = {o.id: o for o in db.scalars(select(VehicleObservation).where(VehicleObservation.id.in_([p.observation_id for p in pts])))}
    points, segments = [], []
    for i, p in enumerate(pts):
        d = point_dict(p)
        o = obs.get(p.observation_id)
        d.update({"camera_name": names.get(p.camera_id), "plate_text": o.plate_text if o else None, "speed_kmh": o.speed_kmh if o else None,
                  "vehicle_class": o.vehicle_class if o else None, "vehicle_color": o.vehicle_color if o else None,
                  "evidence_id": o.evidence_id if o and user.has(Perm.EVIDENCE_READ) else None,
                  "match_reasons": (o.match_reasons or []) if o else []})
        points.append(d)
        prev = pts[i - 1] if i > 0 else None
        if prev is None or p.from_camera_id is None or prev.journey_index != p.journey_index:
            continue
        path = graph.shortest(p.from_camera_id, p.camera_id)
        coords: list[list[float]] = []
        if path is not None:
            for e in path.edges:
                seg = [list(c) for c in (e.path or [])] or [[graph.cameras[e.from_id]["lat"], graph.cameras[e.from_id]["lon"]],
                                                           [graph.cameras[e.to_id]["lat"], graph.cameras[e.to_id]["lon"]]]
                coords.extend(seg if not coords else seg[1:])
        else:
            coords = [[prev.latitude, prev.longitude], [p.latitude, p.longitude]]
        segments.append({"from_seq": prev.seq, "to_seq": p.seq, "from_camera_id": p.from_camera_id, "to_camera_id": p.camera_id,
                         "start": iso(prev.ts), "end": iso(p.ts), "travel_s": p.segment_travel_s, "distance_m": p.segment_distance_m,
                         "speed_kmh": p.segment_speed_kmh, "confidence_level": p.confidence_level, "link_score": p.link_score,
                         "via": path.skipped if path else [], "geometry": coords, "unobserved_cameras": path.skipped if path else []})
    journeys = sorted({p.journey_index for p in pts})
    audit.record(db, user.as_dict(), "vehicle.trajectory_view", "vehicle", v.code, {"journey": journey}, client_ip(request))
    return protect(db, user, {"vehicle": vehicle_dict(v), "journeys": journeys, "points": points, "segments": segments,
                              "start": points[0]["ts"] if points else None, "end": points[-1]["ts"] if points else None,
                              "geometry_source": "camera road graph (offline)",
                              "legend": {"HIGH": "plate and/or strong appearance agreement", "MEDIUM": "partial agreement",
                                         "LOW": "weak link: verify with evidence", "START": "first sighting of a journey"}})

@router.get("/vehicles/{ref}/prediction", summary="Predicted next camera")
def prediction(ref: str, user: CurrentUser = Depends(require(Perm.TRAJECTORY_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    v = _vehicle(db, ref)
    d = vehicle_dict(v)
    hist = db.execute(select(TrajectoryPoint.prediction_status, func.count()).where(TrajectoryPoint.prediction_status.is_not(None),
                                                                                     TrajectoryPoint.global_vehicle_id == v.id)
                      .group_by(TrajectoryPoint.prediction_status)).all()
    net = dict(db.execute(select(TrajectoryPoint.prediction_status, func.count()).where(TrajectoryPoint.prediction_status.is_not(None),
                                                                                         TrajectoryPoint.ts >= datetime.now(timezone.utc) - timedelta(hours=24))
                          .group_by(TrajectoryPoint.prediction_status)).all())
    n = sum(net.values())
    return protect(db, user, {"vehicle_code": v.code, "prediction": d["prediction"], "vehicle_history": dict(hist),
                              "network_accuracy_24h": {"confirmed": net.get("CONFIRMED", 0), "deviated": net.get("DEVIATED", 0),
                                                       "accuracy": round(net.get("CONFIRMED", 0) / n, 3) if n else None}})

