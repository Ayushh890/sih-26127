"""Trajectories: ordered, confidence-annotated camera sightings of a global vehicle,
plus next-camera prediction.

Each :class:`TrajectoryPoint` stores the link that produced it (score, confidence
level, explanation), so a replayed journey shows exactly how certain each hop is.
Predictions are made from the transition history out of the current camera (Laplace-
smoothed counts over the topology's allowed exits) and a time window derived from the
edge's minimum and baseline travel times. When the vehicle next appears the arriving
point records whether the prediction was CONFIRMED or DEVIATED.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Camera, GlobalVehicle, TrajectoryPoint, VehicleObservation
from app.services.identity import MatchResult
from app.services.topology import TopologyGraph


def next_vehicle_code(db: Session) -> str:
    n = (db.scalar(select(func.max(GlobalVehicle.id))) or 0) + 1
    return f"VEH-{n:06d}"


def create_vehicle(db: Session, obs: VehicleObservation) -> GlobalVehicle:
    v = GlobalVehicle(code=next_vehicle_code(db), plate_text=obs.plate_text, plate_confidence=obs.plate_confidence, plate_hash=obs.plate_hash,
                      vehicle_class=obs.vehicle_class, vehicle_color=obs.vehicle_color, first_seen_at=obs.observed_at, last_seen_at=obs.observed_at,
                      first_camera_id=obs.camera_id, last_camera_id=obs.camera_id, observation_count=0, camera_count=0, is_demo=obs.is_demo)
    db.add(v)
    db.flush()
    return v


def append(db: Session, v: GlobalVehicle, obs: VehicleObservation, match: MatchResult, cam: Camera, graph: TopologyGraph) -> TrajectoryPoint:
    last = db.scalar(select(TrajectoryPoint).where(TrajectoryPoint.global_vehicle_id == v.id).order_by(TrajectoryPoint.seq.desc()).limit(1))
    seq = (last.seq + 1) if last else 0
    journey = (last.journey_index + (1 if match.new_journey else 0)) if last else 0
    linked = match.vehicle is not None and not match.new_journey and match.prev is not None
    pt = TrajectoryPoint(global_vehicle_id=v.id, observation_id=obs.id, camera_id=obs.camera_id, ts=obs.observed_at,
                         latitude=cam.latitude, longitude=cam.longitude, seq=seq, journey_index=journey)
    if linked and match.prev is not None:
        pt.from_camera_id = match.prev.camera_id
        pt.segment_travel_s = round(match.dt_s or 0.0, 2)
        if match.path is not None:
            pt.segment_distance_m = round(match.path.distance_m, 1)
            if pt.segment_travel_s and pt.segment_travel_s > 0:
                pt.segment_speed_kmh = round(match.path.distance_m / pt.segment_travel_s * 3.6, 1)
        pt.link_score = match.score
        pt.confidence_level = match.confidence_level
    else:
        pt.confidence_level = "START"
        pt.link_score = match.score
    pt.explanation = "; ".join(match.reasons)[:2000]
    # prediction outcome for the point that arrives
    if v.predicted_camera_id and v.predicted_from_observation_id and linked:
        in_window = v.predicted_window_end is None or obs.observed_at <= v.predicted_window_end + timedelta(seconds=30)
        pt.prediction_status = "CONFIRMED" if (obs.camera_id == v.predicted_camera_id and in_window) else "DEVIATED"
    db.add(pt)
    db.flush()  # autoflush is off: make this observation's vehicle link visible to the queries below

    # update the vehicle summary
    if linked and match.path is not None:
        v.total_distance_m = (v.total_distance_m or 0.0) + match.path.distance_m
    v.last_seen_at = max(v.last_seen_at, obs.observed_at) if v.last_seen_at else obs.observed_at
    v.last_camera_id = obs.camera_id
    v.last_observation_id = obs.id
    v.observation_count = (v.observation_count or 0) + 1
    v.camera_count = db.scalar(select(func.count(func.distinct(VehicleObservation.camera_id))).where(VehicleObservation.global_vehicle_id == v.id)) or 1
    if obs.plate_text and (v.plate_confidence or 0) <= (obs.plate_confidence or 0):
        v.plate_text, v.plate_confidence, v.plate_hash = obs.plate_text, obs.plate_confidence, obs.plate_hash
    if obs.vehicle_class and obs.class_confidence >= 0.5:
        v.vehicle_class = obs.vehicle_class
    if obs.vehicle_color and obs.vehicle_color != "unknown":
        v.vehicle_color = obs.vehicle_color
    db.flush()
    predict_next(db, v, obs, graph)
    return pt


def transition_counts(db: Session, from_camera: str, hours: float = 24.0) -> Counter:
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    rows = db.execute(select(TrajectoryPoint.camera_id, func.count()).where(TrajectoryPoint.from_camera_id == from_camera, TrajectoryPoint.ts >= since,
                                                                           TrajectoryPoint.camera_id != from_camera)
                      .group_by(TrajectoryPoint.camera_id)).all()
    return Counter({c: n for c, n in rows})


def predict_next(db: Session, v: GlobalVehicle, obs: VehicleObservation, graph: TopologyGraph) -> dict[str, Any] | None:
    exits = [e for e in graph.outgoing(obs.camera_id)]
    prev_point = db.scalar(select(TrajectoryPoint).where(TrajectoryPoint.observation_id == obs.id))
    came_from = prev_point.from_camera_id if prev_point else None
    # a vehicle rarely turns straight back: prefer exits other than where it came from
    if came_from and len(exits) > 1:
        exits = [e for e in exits if e.to_id != came_from]
    if not exits:
        v.predicted_camera_id = v.predicted_window_start = v.predicted_window_end = v.predicted_probability = None
        v.predicted_from_observation_id = None
        return None
    counts = transition_counts(db, obs.camera_id)
    total = sum(counts.get(e.to_id, 0) for e in exits)
    probs = {e.to_id: (counts.get(e.to_id, 0) + 1) / (total + len(exits)) for e in exits}
    best = max(exits, key=lambda e: probs[e.to_id])
    v.predicted_camera_id = best.to_id
    v.predicted_from_observation_id = obs.id
    v.predicted_probability = round(probs[best.to_id], 3)
    v.predicted_window_start = obs.observed_at + timedelta(seconds=best.min_travel_s)
    v.predicted_window_end = obs.observed_at + timedelta(seconds=max(best.typical_travel_s * 2.5, best.min_travel_s + 60))
    return {"camera_id": best.to_id, "probability": v.predicted_probability, "window_start": v.predicted_window_start.isoformat(),
            "window_end": v.predicted_window_end.isoformat(), "alternatives": {k: round(p, 3) for k, p in probs.items()},
            "basis": f"{total} historical transitions from {obs.camera_id}" if total else "no history yet: topology prior (uniform over exits)"}


def prediction_state(v: GlobalVehicle, now: datetime | None = None) -> dict[str, Any] | None:
    if not v.predicted_camera_id:
        return None
    now = now or datetime.now(timezone.utc)
    status = "PENDING"
    if v.predicted_window_end and now > v.predicted_window_end + timedelta(seconds=30):
        status = "EXPIRED"
    return {"camera_id": v.predicted_camera_id, "probability": v.predicted_probability, "status": status,
            "window_start": v.predicted_window_start.isoformat() if v.predicted_window_start else None,
            "window_end": v.predicted_window_end.isoformat() if v.predicted_window_end else None,
            "from_observation_id": v.predicted_from_observation_id}
