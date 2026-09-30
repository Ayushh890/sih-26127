"""Alert rules. Each rule is explicit, parameterised from the ``alerts`` settings section
and records the evidence that triggered it in the alert's ``details``.

Per-observation rules run inside ingestion (same transaction as the observation):
WATCHLIST_MATCH, WRONG_WAY, IMPOSSIBLE_TRAVEL, REPEATED_SIGHTING.

Periodic rules run on the scheduler: SEVERE_CONGESTION, CAMERA_DEGRADED,
OCR_DEGRADATION, TRAFFIC_SURGE, TRAVEL_TIME_ANOMALY.

CAMERA_OFFLINE is raised from camera status events (ingestion) and auto-resolved when
the camera is ONLINE again.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import Camera, GlobalVehicle, PlateRead, SystemEvent, TrafficMetric, VehicleObservation, VehicleTrack
from app.services import analytics
from app.services.alerts import AlertEngine
from app.services.identity import MatchResult
from app.services.topology import TopologyGraph
from app.services.watchlist import active_entries, match_plate

log = get_logger("alert_rules")

PRIORITY_SEVERITY = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH", "CRITICAL": "CRITICAL"}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# --------------------------------------------------------------------------- per observation
def on_observation(engine: AlertEngine, db: Session, cfg: dict[str, Any], obs: VehicleObservation, cam: Camera,
                   vehicle: GlobalVehicle, match: MatchResult) -> list[Any]:
    raised = []
    for rule in (_watchlist, _wrong_way, _impossible_travel, _repeated_sighting):
        try:
            a = rule(engine, db, cfg, obs, cam, vehicle, match)
            if a is not None:
                raised.append(a)
        except Exception:  # one faulty rule must not block the others or the observation
            log.exception("alert rule %s failed", rule.__name__, extra={"camera_id": obs.camera_id})
    return raised


def _watchlist(engine: AlertEngine, db: Session, cfg: dict[str, Any], obs: VehicleObservation, cam: Camera, v: GlobalVehicle, m: MatchResult):
    rule = cfg["WATCHLIST_MATCH"]
    if not rule.get("enabled", True) or not obs.plate_text:
        return None
    hits = match_plate(active_entries(db, obs.observed_at), obs.plate_text, float(rule.get("fuzzy_max_distance", 1.0)))
    last = None
    for entry, dist in hits:
        entry.last_match_at = obs.observed_at
        entry.match_count = (entry.match_count or 0) + 1
        kind = "exact match" if dist == 0 else f"fuzzy match (OCR-confusion distance {dist})"
        last = engine.raise_alert(
            db, cfg, type="WATCHLIST_MATCH", severity=PRIORITY_SEVERITY.get(entry.priority, "MEDIUM"),
            title=f"Watchlist {kind}: {obs.plate_text} at {cam.name}",
            reason=f"Plate {obs.plate_text} read at {cam.id} ({obs.plate_confidence or 0:.0%} OCR confidence) is a {kind} for watchlist entry "
                   f"#{entry.id} ({entry.plate}) — reason on file: {entry.reason}",
            details={"watchlist_id": entry.id, "watchlist_plate": entry.plate, "watchlist_reason": entry.reason, "priority": entry.priority,
                     "match_mode": entry.match_mode, "distance": dist, "plate_text": obs.plate_text, "plate_raw": obs.plate_raw,
                     "ocr_confidence": obs.plate_confidence, "vehicle_code": v.code, "observed_at": obs.observed_at.isoformat()},
            dedup_key=f"WATCHLIST:{entry.id}:{obs.camera_id}", camera_id=obs.camera_id, global_vehicle_id=v.id, observation_id=obs.id,
            confidence=obs.plate_confidence, evidence_id=obs.evidence_id, is_demo=obs.is_demo)
    return last


def _wrong_way(engine: AlertEngine, db: Session, cfg: dict[str, Any], obs: VehicleObservation, cam: Camera, v: GlobalVehicle, m: MatchResult):
    rule = cfg["WRONG_WAY"]
    if obs.motion != "against_flow" or not (cam.calibration or {}).get("one_way"):
        return None
    track = db.get(VehicleTrack, obs.track_id) if obs.track_id is not None else None
    track_frames = track.frames if track is not None else None
    if track_frames is not None and track_frames < int(rule.get("min_track_frames", 4)):
        return None
    ident = obs.plate_text or v.code
    return engine.raise_alert(
        db, cfg, type="WRONG_WAY", title=f"Wrong-way vehicle {ident} on {cam.road_name or cam.name}",
        reason=f"{obs.vehicle_class} tracked for {track_frames} frames moving against the permitted direction ({cam.direction}) of one-way "
               f"road at {cam.id}; estimated heading {obs.direction or 'unknown'}"
               + (f", speed {obs.speed_kmh:.0f} km/h" if obs.speed_kmh is not None else ""),
        details={"motion": obs.motion, "heading": obs.direction, "permitted_direction": cam.direction, "track_frames": track_frames,
                 "speed_kmh": obs.speed_kmh, "plate_text": obs.plate_text, "vehicle_code": v.code, "lane": obs.lane},
        dedup_key=f"WRONG_WAY:{obs.camera_id}:{obs.local_track_id}", camera_id=obs.camera_id, global_vehicle_id=v.id, observation_id=obs.id,
        confidence=obs.class_confidence, evidence_id=obs.evidence_id, is_demo=obs.is_demo)


def _impossible_travel(engine: AlertEngine, db: Session, cfg: dict[str, Any], obs: VehicleObservation, cam: Camera, v: GlobalVehicle, m: MatchResult):
    rule = cfg["IMPOSSIBLE_TRAVEL"]
    last = None
    for c in m.conflicts:
        confs = [x for x in c["plate_confidence"] if x is not None]
        if len(confs) < 2 or min(confs) < float(rule["min_plate_confidence"]):
            continue
        if not c.get("min_travel_s"):
            continue
        ratio = c["dt_s"] / c["min_travel_s"]
        if ratio > float(rule["max_ratio"]):
            continue
        attr_diff = []
        if c["vehicle_class"][0] != c["vehicle_class"][1]:
            attr_diff.append(f"class {c['vehicle_class'][0]} vs {c['vehicle_class'][1]}")
        if c["vehicle_color"][0] != c["vehicle_color"][1]:
            attr_diff.append(f"colour {c['vehicle_color'][0]} vs {c['vehicle_color'][1]}")
        prev_obs = db.get(VehicleObservation, c["observation_id"])
        last = engine.raise_alert(
            db, cfg, type="IMPOSSIBLE_TRAVEL", title=f"Impossible travel: plate {obs.plate_text} at {c['camera_id']} and {obs.camera_id}",
            reason=f"Plate {obs.plate_text} was read at {c['camera_id']} and {obs.camera_id} {c['dt_s']:.0f}s apart, but the shortest allowed road "
                   f"path ({(c['distance_m'] or 0) / 1000:.2f} km) needs at least {c['min_travel_s']:.0f}s. Possible cloned or misread plate"
                   + (f"; appearance also differs ({', '.join(attr_diff)})" if attr_diff else ""),
            details={"plate_text": obs.plate_text, "first": {"camera_id": c["camera_id"], "observation_id": c["observation_id"], "observed_at": c["observed_at"],
                                                             "vehicle_code": c["vehicle_code"], "evidence_id": prev_obs.evidence_id if prev_obs else None},
                     "second": {"camera_id": obs.camera_id, "observation_id": obs.id, "observed_at": obs.observed_at.isoformat(), "vehicle_code": v.code,
                                "evidence_id": obs.evidence_id},
                     "dt_s": c["dt_s"], "min_travel_s": c["min_travel_s"], "distance_m": c["distance_m"], "ratio": round(ratio, 3),
                     "plate_confidence": c["plate_confidence"], "attribute_differences": attr_diff},
            dedup_key=f"IMPOSSIBLE_TRAVEL:{obs.plate_hash or obs.plate_text}", camera_id=obs.camera_id, global_vehicle_id=v.id, observation_id=obs.id,
            confidence=min(confs), evidence_id=obs.evidence_id, is_demo=obs.is_demo)
    return last


def _repeated_sighting(engine: AlertEngine, db: Session, cfg: dict[str, Any], obs: VehicleObservation, cam: Camera, v: GlobalVehicle, m: MatchResult):
    rule = cfg["REPEATED_SIGHTING"]
    if not rule.get("enabled", True):
        return None
    window = timedelta(seconds=float(rule["window_s"]))
    same = [VehicleObservation.global_vehicle_id == v.id]
    if obs.plate_text and (obs.plate_confidence or 0) >= 0.6:
        same.append(VehicleObservation.plate_text == obs.plate_text)
    rows = db.scalars(select(VehicleObservation).where(VehicleObservation.camera_id == obs.camera_id, or_(*same),
                                                       VehicleObservation.observed_at >= obs.observed_at - window,
                                                       VehicleObservation.observed_at <= obs.observed_at).order_by(VehicleObservation.observed_at)).all()
    # passes closer together than 60 s are fragments of the same passage, not repeat visits
    passes: list[VehicleObservation] = []
    for r in rows:
        if not passes or (_utc(r.observed_at) - _utc(passes[-1].observed_at)).total_seconds() >= 60:
            passes.append(r)
    if len(passes) < int(rule["min_sightings"]):
        return None
    ident = obs.plate_text or v.code
    times = [_utc(p.observed_at).strftime("%H:%M:%S") for p in passes]
    return engine.raise_alert(
        db, cfg, type="REPEATED_SIGHTING", title=f"{ident} passed {cam.name} {len(passes)} times",
        reason=f"{ident} was seen at {cam.id} {len(passes)} times within {rule['window_s'] // 60:.0f} min ({', '.join(times)}); "
               f"threshold is {rule['min_sightings']} sightings",
        details={"sightings": [{"observation_id": p.id, "observed_at": p.observed_at.isoformat(), "evidence_id": p.evidence_id} for p in passes],
                 "plate_text": obs.plate_text, "vehicle_code": v.code, "window_s": rule["window_s"]},
        dedup_key=f"REPEATED:{obs.camera_id}:{obs.plate_hash or v.id}", camera_id=obs.camera_id, global_vehicle_id=v.id, observation_id=obs.id,
        confidence=m.score if m.score is not None else obs.plate_confidence, evidence_id=obs.evidence_id, is_demo=obs.is_demo)


# --------------------------------------------------------------------------- camera status
def on_camera_status(engine: AlertEngine, db: Session, cfg: dict[str, Any], cam: Camera, status: str, message: str) -> None:
    key = f"CAMERA_OFFLINE:{cam.id}"
    if status in ("OFFLINE", "ERROR"):
        engine.raise_alert(db, cfg, type="CAMERA_OFFLINE", title=f"Camera {cam.id} {status.lower()}: {cam.name}",
                           reason=f"{cam.name} ({cam.id}) went {status}: {message or 'no frames received'}",
                           details={"status": status, "message": message, "source_type": cam.source_type},
                           dedup_key=key, camera_id=cam.id, is_demo=cam.is_demo)
    elif status == "ONLINE":
        engine.auto_resolve(db, key, "camera back ONLINE")
        engine.auto_resolve(db, f"CAMERA_DEGRADED:{cam.id}", "camera back ONLINE")


# --------------------------------------------------------------------------- periodic
def run_periodic(engine: AlertEngine, db: Session, cfg: dict[str, Any], congestion_cfg: dict[str, Any], graph: TopologyGraph,
                 cameras: list[Camera], congestion: list[dict[str, Any]] | None = None, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    n = 0
    for fn in (_severe_congestion, _camera_degraded, _ocr_degradation, _traffic_surge, _travel_time_anomaly):
        try:
            n += fn(engine, db, cfg, congestion_cfg, graph, cameras, congestion, now)
        except Exception:
            log.exception("periodic alert rule %s failed", fn.__name__)
    return n


def _severe_congestion(engine, db, cfg, ccfg, graph, cameras, congestion, now) -> int:
    rule = cfg["SEVERE_CONGESTION"]
    if not rule.get("enabled", True):
        return 0
    congestion = congestion if congestion is not None else analytics.network_congestion(db, ccfg, graph, [c.id for c in cameras], now)
    by_id = {c.id: c for c in cameras}
    n = 0
    for c in congestion:
        cam = by_id.get(c["camera_id"])
        if cam is None or c["score"] is None:
            continue
        key = f"SEVERE_CONGESTION:{cam.id}"
        if c["score"] >= float(rule["min_score"]):
            engine.raise_alert(db, cfg, type="SEVERE_CONGESTION", title=f"Severe congestion at {cam.name}",
                               reason=f"Congestion score {c['score']:.0f}/100 (≥ {rule['min_score']}) at {cam.id}: " + "; ".join(c["explanation"][1:4]),
                               details={"score": c["score"], "level": c["level"], "components": c["components"], "explanation": c["explanation"],
                                        "metrics": c["metrics"]},
                               dedup_key=key, camera_id=cam.id, confidence=None, is_demo=cam.is_demo, when=now)
            n += 1
        elif c["score"] < float(ccfg["thresholds"]["heavy"]):
            engine.auto_resolve(db, key, f"congestion eased to {c['level']} ({c['score']:.0f}/100)")
    return n


def _status_since(db: Session, cam: Camera) -> datetime | None:
    ev = db.scalar(select(SystemEvent).where(SystemEvent.camera_id == cam.id, SystemEvent.event_type == "camera_status")
                   .order_by(SystemEvent.ts.desc()).limit(1))
    return _utc(ev.ts) if ev else None


def _camera_degraded(engine, db, cfg, ccfg, graph, cameras, congestion, now) -> int:
    rule = cfg["CAMERA_DEGRADED"]
    if not rule.get("enabled", True):
        return 0
    n = 0
    for cam in cameras:
        if cam.status != "DEGRADED":
            continue
        since = _status_since(db, cam)
        if since is None or (now - since).total_seconds() < float(rule["min_duration_s"]):
            continue
        engine.raise_alert(db, cfg, type="CAMERA_DEGRADED", title=f"Camera {cam.id} degraded: {cam.name}",
                           reason=f"{cam.id} has been DEGRADED for {(now - since).total_seconds():.0f}s: {cam.status_message}",
                           details={"since": since.isoformat(), "message": cam.status_message},
                           dedup_key=f"CAMERA_DEGRADED:{cam.id}", camera_id=cam.id, is_demo=cam.is_demo, when=now)
        n += 1
    return n


def _ocr_degradation(engine, db, cfg, ccfg, graph, cameras, congestion, now) -> int:
    rule = cfg["OCR_DEGRADATION"]
    if not rule.get("enabled", True):
        return 0
    recent_s, window_s = float(rule["recent_s"]), float(rule["window_s"])
    n = 0
    for cam in cameras:
        def stats(a: datetime, b: datetime) -> tuple[int, float | None]:
            cnt, avg = db.execute(select(func.count(), func.avg(PlateRead.confidence)).where(
                PlateRead.camera_id == cam.id, PlateRead.ts >= a, PlateRead.ts < b)).one()
            return int(cnt or 0), (float(avg) if avg is not None else None)

        rc, ravg = stats(now - timedelta(seconds=recent_s), now)
        bc, bavg = stats(now - timedelta(seconds=window_s), now - timedelta(seconds=recent_s))
        key = f"OCR_DEGRADATION:{cam.id}"
        if rc < int(rule["min_reads"]) or bc < int(rule["min_reads"]) or ravg is None or not bavg:
            continue
        drop = (bavg - ravg) / bavg
        if drop >= float(rule["drop_ratio"]):
            engine.raise_alert(db, cfg, type="OCR_DEGRADATION", title=f"OCR confidence dropped at {cam.name}",
                               reason=f"Mean OCR confidence at {cam.id} fell from {bavg:.0%} to {ravg:.0%} ({drop:.0%} drop, {rc} recent reads). "
                                      "Check focus, lens cleanliness, lighting or camera angle.",
                               details={"recent_mean": round(ravg, 4), "baseline_mean": round(bavg, 4), "recent_reads": rc, "baseline_reads": bc,
                                        "drop": round(drop, 3), "recent_s": recent_s, "window_s": window_s},
                               dedup_key=key, camera_id=cam.id, is_demo=cam.is_demo, when=now)
            n += 1
        elif drop < float(rule["drop_ratio"]) / 2:
            engine.auto_resolve(db, key, f"OCR confidence recovered ({ravg:.0%})")
    return n


def _traffic_surge(engine, db, cfg, ccfg, graph, cameras, congestion, now) -> int:
    rule = cfg["TRAFFIC_SURGE"]
    if not rule.get("enabled", True):
        return 0
    window = float(rule["window_s"])
    n = 0
    for cam in cameras:
        def count(a: datetime, b: datetime) -> tuple[int, float]:
            cnt, secs = db.execute(select(func.coalesce(func.sum(TrafficMetric.new_tracks), 0), func.coalesce(func.sum(TrafficMetric.bucket_seconds), 0))
                                   .where(TrafficMetric.camera_id == cam.id, TrafficMetric.bucket_start >= a, TrafficMetric.bucket_start < b)).one()
            return int(cnt), float(secs)

        cur, cur_s = count(now - timedelta(seconds=window), now)
        base, base_s = count(now - timedelta(seconds=float(ccfg["baseline_s"])), now - timedelta(seconds=window))
        if cur < int(rule["min_count"]) or base_s < 2 * window or base == 0 or cur_s <= 0:
            continue
        cur_rate, base_rate = cur / cur_s * 60, base / base_s * 60
        ratio = cur_rate / base_rate
        if ratio >= float(rule["ratio"]):
            engine.raise_alert(db, cfg, type="TRAFFIC_SURGE", title=f"Traffic surge at {cam.name}",
                               reason=f"{cur_rate:.1f} vehicles/min at {cam.id} over the last {window / 60:.0f} min vs baseline {base_rate:.1f}/min ({ratio:.1f}×)",
                               details={"current_rate_per_min": round(cur_rate, 2), "baseline_rate_per_min": round(base_rate, 2), "ratio": round(ratio, 2),
                                        "current_count": cur, "baseline_seconds": base_s},
                               dedup_key=f"TRAFFIC_SURGE:{cam.id}", camera_id=cam.id, is_demo=cam.is_demo, when=now)
            n += 1
    return n


def _travel_time_anomaly(engine, db, cfg, ccfg, graph, cameras, congestion, now) -> int:
    rule = cfg["TRAVEL_TIME_ANOMALY"]
    if not rule.get("enabled", True):
        return 0
    by_id = {c.id: c for c in cameras}
    n = 0
    for a in analytics.travel_time_anomalies(db, graph, list(by_id), float(rule["window_s"]), float(rule["ratio"]), int(rule["min_samples"]),
                                             float(ccfg["baseline_s"]), now):
        key = f"TRAVEL_TIME:{a['from']}:{a['to']}"
        if a["anomalous"]:
            cam = by_id[a["to"]]
            engine.raise_alert(db, cfg, type="TRAVEL_TIME_ANOMALY", title=f"Slow travel {a['from']} → {a['to']}",
                               reason=a["explanation"] + f" (threshold {rule['ratio']}×)", details=a, dedup_key=key, camera_id=a["to"],
                               is_demo=cam.is_demo, when=now)
            n += 1
        elif a["ratio"] < 1.2:
            engine.auto_resolve(db, key, f"travel time back to {a['ratio']:.1f}× baseline")
    return n
