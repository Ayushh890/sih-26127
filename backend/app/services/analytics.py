"""Traffic analytics computed from persisted data (``traffic_metrics``, observations and
trajectory segments). Nothing here is simulated: an empty database yields empty results.

Congestion score (0–100) per camera, over the configured window, is a weighted mean of
the components that have data (weights from the ``congestion`` settings section):

* speed        – 1 − mean speed / free-flow speed
* occupancy    – share of the road area covered by vehicles / occupancy saturation
* queue        – queued (slow or stopped) vehicles in view / queue saturation
* volume       – vehicle rate vs. the camera's own baseline rate
* travel_time  – inbound segment travel times vs. per-edge baselines

Each component is reported with its raw value and a plain-language explanation, and the
score maps to FREE / MODERATE / HEAVY / SEVERE via configurable thresholds.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Alert, Camera, TrafficMetric, TrajectoryPoint, VehicleObservation
from app.services.topology import TopologyGraph
from app.services.travel_times import baselines, segment_samples

LEVELS = ("FREE", "MODERATE", "HEAVY", "SEVERE")


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def level_for_score(score: float, thresholds: dict[str, float]) -> str:
    if score >= thresholds["severe"]:
        return "SEVERE"
    if score >= thresholds["heavy"]:
        return "HEAVY"
    if score >= thresholds["moderate"]:
        return "MODERATE"
    return "FREE"


def _metrics(db: Session, camera_ids: list[str], since: datetime, until: datetime) -> list[TrafficMetric]:
    if not camera_ids:
        return []
    return list(db.scalars(select(TrafficMetric).where(TrafficMetric.camera_id.in_(camera_ids), TrafficMetric.bucket_start >= since,
                                                        TrafficMetric.bucket_start < until).order_by(TrafficMetric.bucket_start)))


def aggregate(rows: list[TrafficMetric]) -> dict[str, Any] | None:
    """Combine per-minute buckets into window statistics (frame-weighted means)."""
    if not rows:
        return None
    frames = sum(r.frames for r in rows) or 0
    seconds = sum(r.bucket_seconds for r in rows) or 1

    def fw(attr: str) -> float:
        if frames == 0:
            return 0.0
        return sum(getattr(r, attr) * r.frames for r in rows) / frames

    sp = [(r.avg_speed_kmh, max(1, r.new_tracks)) for r in rows if r.avg_speed_kmh is not None]
    speed = sum(s * w for s, w in sp) / sum(w for _, w in sp) if sp else None
    count = sum(r.new_tracks for r in rows)
    return {
        "vehicles": count,
        "rate_per_min": round(count / (seconds / 60.0), 2),
        "avg_speed_kmh": round(speed, 1) if speed is not None else None,
        "occupancy": round(fw("occupancy"), 4),
        "queue": round(fw("avg_stationary"), 2),
        "density": round(fw("avg_vehicles_in_frame"), 2),
        "max_in_frame": max(r.max_vehicles_in_frame for r in rows),
        "buckets": len(rows),
        "covered_s": seconds,
        "from": rows[0].bucket_start.isoformat(),
        "to": (rows[-1].bucket_start + timedelta(seconds=rows[-1].bucket_seconds)).isoformat(),
    }


def _inbound_travel(db: Session, graph: TopologyGraph, camera_id: str, since: datetime, until: datetime, base: dict) -> dict[str, Any] | None:
    samples = segment_samples(db, since, until)
    ratios, detail = [], []
    for e in graph.incoming(camera_id):
        s = samples.get((e.from_id, camera_id))
        b = base.get((e.from_id, camera_id))
        if not s or not b:
            continue
        med = statistics.median(s)
        ratios.append((med / max(b["seconds"], 1.0), len(s)))
        detail.append({"from": e.from_id, "median_s": round(med, 1), "baseline_s": round(b["seconds"], 1), "baseline_source": b["source"], "samples": len(s)})
    if not ratios:
        return None
    ratio = sum(r * n for r, n in ratios) / sum(n for _, n in ratios)
    return {"ratio": round(ratio, 2), "segments": detail}


def camera_congestion(db: Session, cfg: dict[str, Any], graph: TopologyGraph, cam: Camera, now: datetime | None = None,
                      base: dict | None = None) -> dict[str, Any]:
    asof, now = now, now or datetime.now(timezone.utc)
    window = timedelta(seconds=cfg["window_s"])
    cur = aggregate(_metrics(db, [cam.id], now - window, now))
    out: dict[str, Any] = {"camera_id": cam.id, "camera_name": cam.name, "latitude": cam.latitude, "longitude": cam.longitude,
                           "window_s": cfg["window_s"], "status": cam.status, "is_demo": cam.is_demo}
    if cur is None:
        return {**out, "score": None, "level": "NO_DATA", "components": {}, "explanation": ["no traffic data in the analysis window"], "metrics": None}
    w = cfg["weights"]
    comps: dict[str, dict[str, Any]] = {}
    free = float(cfg["free_flow_speed_kmh"])
    if cur["avg_speed_kmh"] is not None:
        v = min(1.0, max(0.0, 1.0 - cur["avg_speed_kmh"] / free))
        comps["speed"] = {"value": v, "raw": cur["avg_speed_kmh"],
                          "text": f"average speed {cur['avg_speed_kmh']:.0f} km/h vs free-flow {free:.0f} km/h"}
    occ_sat = float(cfg.get("occupancy_saturation", 0.35))
    comps["occupancy"] = {"value": min(1.0, cur["occupancy"] / occ_sat), "raw": cur["occupancy"],
                          "text": f"vehicles cover {cur['occupancy']:.0%} of the monitored road area (saturation {occ_sat:.0%})"}
    q_sat = float(cfg["queue_saturation"])
    comps["queue"] = {"value": min(1.0, cur["queue"] / q_sat), "raw": cur["queue"],
                      "text": f"{cur['queue']:.1f} queued vehicles in view on average (queue saturation {q_sat:.0f})"}
    base_rows = _metrics(db, [cam.id], now - timedelta(seconds=cfg["baseline_s"]), now - window)
    b = aggregate(base_rows)
    if b is not None and b["covered_s"] >= 2 * cfg["window_s"] and b["rate_per_min"] > 0:
        ratio = cur["rate_per_min"] / b["rate_per_min"]
        comps["volume"] = {"value": min(1.0, max(0.0, ratio - 1.0)), "raw": round(ratio, 2),
                           "text": f"{cur['rate_per_min']:.1f} vehicles/min vs baseline {b['rate_per_min']:.1f} ({ratio:.1f}×)"}
    base = base if base is not None else baselines(db, graph, cfg["baseline_s"], cfg["window_s"], now=asof)
    tt = _inbound_travel(db, graph, cam.id, now - window, now, base)
    if tt is not None:
        comps["travel_time"] = {"value": min(1.0, max(0.0, (tt["ratio"] - 1.0) / 1.5)), "raw": tt["ratio"], "segments": tt["segments"],
                                "text": f"inbound travel times are {tt['ratio']:.1f}× their baseline"}
    total_w = sum(w[k] for k in comps)
    score = 100.0 * sum(w[k] * comps[k]["value"] for k in comps) / total_w if total_w else 0.0
    level = level_for_score(score, cfg["thresholds"])
    ranked = sorted(comps.items(), key=lambda kv: w[kv[0]] * kv[1]["value"], reverse=True)
    explanation = [f"{level} ({score:.0f}/100) over the last {cfg['window_s'] // 60} min"]
    explanation += [f"{v['text']} → {k} {v['value']:.0%}" for k, v in ranked]
    missing = [k for k in w if k not in comps]
    if missing:
        explanation.append(f"not enough data for: {', '.join(missing)} (weights renormalised)")
    for v in comps.values():
        v["value"] = round(v["value"], 3)
    return {**out, "score": round(score, 1), "level": level, "components": comps, "weights": {k: w[k] for k in comps},
            "explanation": explanation, "metrics": cur}


def network_congestion(db: Session, cfg: dict[str, Any], graph: TopologyGraph, camera_ids: list[str], now: datetime | None = None) -> list[dict[str, Any]]:
    asof, now = now, now or datetime.now(timezone.utc)
    base = baselines(db, graph, cfg["baseline_s"], cfg["window_s"], now=asof)
    cams = db.scalars(select(Camera).where(Camera.id.in_(camera_ids)).order_by(Camera.id)) if camera_ids else []
    return [camera_congestion(db, cfg, graph, c, now, base) for c in cams]


def time_series(db: Session, camera_ids: list[str], since: datetime, until: datetime, bucket_s: int = 300) -> list[dict[str, Any]]:
    rows = _metrics(db, camera_ids, since, until)
    groups: dict[datetime, list[TrafficMetric]] = defaultdict(list)
    for r in rows:
        ts = _utc(r.bucket_start)
        key = datetime.fromtimestamp(int(ts.timestamp()) // bucket_s * bucket_s, tz=timezone.utc)
        groups[key].append(r)
    out = []
    for k in sorted(groups):
        a = aggregate(groups[k])
        if a:
            out.append({"ts": k.isoformat(), "vehicles": a["vehicles"], "rate_per_min": a["rate_per_min"], "avg_speed_kmh": a["avg_speed_kmh"],
                        "occupancy": a["occupancy"], "queue": a["queue"], "density": a["density"]})
    return out


def summary(db: Session, camera_ids: list[str], since: datetime, until: datetime) -> dict[str, Any]:
    rows = _metrics(db, camera_ids, since, until)
    per_cam: dict[str, list[TrafficMetric]] = defaultdict(list)
    for r in rows:
        per_cam[r.camera_id].append(r)
    total = aggregate(rows)
    obs_q = select(VehicleObservation.vehicle_class, func.count()).where(VehicleObservation.camera_id.in_(camera_ids or [""]),
                                                                          VehicleObservation.observed_at >= since, VehicleObservation.observed_at < until)
    classes = {c: n for c, n in db.execute(obs_q.group_by(VehicleObservation.vehicle_class))}
    plated = db.scalar(select(func.count()).select_from(VehicleObservation).where(
        VehicleObservation.camera_id.in_(camera_ids or [""]), VehicleObservation.observed_at >= since, VehicleObservation.observed_at < until,
        VehicleObservation.plate_text.is_not(None))) or 0
    n_obs = sum(classes.values())
    return {
        "totals": total,
        "observations": n_obs,
        "plate_read_rate": round(plated / n_obs, 3) if n_obs else None,
        "by_class": classes,
        "by_camera": {cid: aggregate(rs) for cid, rs in sorted(per_cam.items())},
    }


def hotspots(db: Session, cfg: dict[str, Any], graph: TopologyGraph, camera_ids: list[str], since: datetime, until: datetime) -> list[dict[str, Any]]:
    """Cameras ranked by period congestion (mean of 5-min scores) and alert load."""
    cams = {c.id: c for c in db.scalars(select(Camera).where(Camera.id.in_(camera_ids or [""])))}
    alert_counts = dict(db.execute(select(Alert.camera_id, func.count()).where(Alert.camera_id.in_(camera_ids or [""]), Alert.created_at >= since,
                                                                               Alert.created_at < until).group_by(Alert.camera_id)).all())
    rows = _metrics(db, list(cams), since, until)
    per_cam: dict[str, list[TrafficMetric]] = defaultdict(list)
    for r in rows:
        per_cam[r.camera_id].append(r)
    out = []
    free, occ_sat, q_sat = float(cfg["free_flow_speed_kmh"]), float(cfg.get("occupancy_saturation", 0.35)), float(cfg["queue_saturation"])
    for cid, rs in per_cam.items():
        a = aggregate(rs)
        if a is None:
            continue
        parts = {"occupancy": min(1.0, a["occupancy"] / occ_sat), "queue": min(1.0, a["queue"] / q_sat)}
        if a["avg_speed_kmh"] is not None:
            parts["speed"] = max(0.0, 1.0 - a["avg_speed_kmh"] / free)
        w = cfg["weights"]
        score = 100 * sum(w[k] * v for k, v in parts.items()) / sum(w[k] for k in parts)
        # peak 5-min severity inside the period
        peak = max((100 * min(1.0, r.avg_stationary / q_sat) for r in rs), default=0.0)
        c = cams[cid]
        out.append({"camera_id": cid, "camera_name": c.name, "latitude": c.latitude, "longitude": c.longitude, "score": round(score, 1),
                    "level": level_for_score(score, cfg["thresholds"]), "peak_queue_score": round(peak, 1), "alerts": int(alert_counts.get(cid, 0)),
                    "vehicles": a["vehicles"], "avg_speed_kmh": a["avg_speed_kmh"], "queue": a["queue"], "occupancy": a["occupancy"]})
    return sorted(out, key=lambda h: (h["score"], h["alerts"]), reverse=True)


def journeys(db: Session, camera_ids: list[str], since: datetime, until: datetime) -> dict[tuple[int, int], list[TrajectoryPoint]]:
    pts = db.scalars(select(TrajectoryPoint).where(TrajectoryPoint.ts >= since, TrajectoryPoint.ts < until, TrajectoryPoint.camera_id.in_(camera_ids or [""]))
                     .order_by(TrajectoryPoint.global_vehicle_id, TrajectoryPoint.seq))
    out: dict[tuple[int, int], list[TrajectoryPoint]] = defaultdict(list)
    for p in pts:
        out[(p.global_vehicle_id, p.journey_index)].append(p)
    return out


def od_matrix(db: Session, camera_ids: list[str], since: datetime, until: datetime, min_count: int = 1) -> dict[str, Any]:
    """Origin → destination counts of multi-camera journeys (first → last camera).

    Cells with fewer than ``min_count`` journeys are suppressed so small counts cannot
    single out individual vehicles; suppressed totals are reported separately.
    """
    cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for pts in journeys(db, camera_ids, since, until).values():
        if len(pts) < 2 or pts[0].camera_id == pts[-1].camera_id:
            continue
        cells[(pts[0].camera_id, pts[-1].camera_id)].append((_utc(pts[-1].ts) - _utc(pts[0].ts)).total_seconds())
    matrix, suppressed = [], 0
    for (o, d), durs in sorted(cells.items()):
        if len(durs) < min_count:
            suppressed += len(durs)
            continue
        matrix.append({"origin": o, "destination": d, "count": len(durs), "median_travel_s": round(statistics.median(durs), 1)})
    return {"cells": matrix, "suppressed_journeys": suppressed, "min_count": min_count, "cameras": sorted(camera_ids),
            "total_journeys": sum(len(v) for v in cells.values())}


def od_cell_vehicles(db: Session, camera_ids: list[str], since: datetime, until: datetime, origin: str, destination: str, limit: int = 200) -> list[dict[str, Any]]:
    out = []
    for (gv, j), pts in journeys(db, camera_ids, since, until).items():
        if len(pts) >= 2 and pts[0].camera_id == origin and pts[-1].camera_id == destination:
            out.append({"global_vehicle_id": gv, "journey_index": j, "start": pts[0].ts.isoformat(), "end": pts[-1].ts.isoformat(),
                        "cameras": [p.camera_id for p in pts]})
            if len(out) >= limit:
                break
    return out


def travel_time_anomalies(db: Session, graph: TopologyGraph, camera_ids: list[str], window_s: float, ratio: float, min_samples: int,
                          baseline_s: float, now: datetime | None = None) -> list[dict[str, Any]]:
    asof, now = now, now or datetime.now(timezone.utc)
    base = baselines(db, graph, baseline_s, window_s, now=asof)
    recent = segment_samples(db, now - timedelta(seconds=window_s), now)
    allowed = set(camera_ids)
    out = []
    for (a, b), s in recent.items():
        if a not in allowed or b not in allowed or len(s) < min_samples:
            continue
        bl = base.get((a, b))
        if not bl:
            continue
        med = statistics.median(s)
        r = med / max(bl["seconds"], 1.0)
        out.append({"from": a, "to": b, "median_s": round(med, 1), "baseline_s": round(bl["seconds"], 1), "baseline_source": bl["source"],
                    "ratio": round(r, 2), "samples": len(s), "anomalous": r >= ratio,
                    "explanation": f"{a} → {b}: median {med:.0f}s over {len(s)} linked vehicles vs baseline {bl['seconds']:.0f}s ({bl['source']}) = {r:.1f}×"})
    return sorted(out, key=lambda x: x["ratio"], reverse=True)


def incident_impact(db: Session, cfg: dict[str, Any], graph: TopologyGraph, camera_id: str, start: datetime, end: datetime) -> dict[str, Any]:
    """Compare an incident window at a camera (and its upstream cameras) with the preceding period of equal length."""
    dur = end - start
    if dur.total_seconds() <= 0:
        raise ValueError("end must be after start")
    before = (start - max(dur, timedelta(minutes=15)), start)
    # neighbours from the other data scope (live vs synthetic) are never mixed into the comparison
    demo = db.scalar(select(Camera.is_demo).where(Camera.id == camera_id))
    same = set(db.scalars(select(Camera.id).where(Camera.is_demo.is_(bool(demo)))))
    upstream = sorted({e.from_id for e in graph.incoming(camera_id)} & same)
    downstream = sorted({e.to_id for e in graph.outgoing(camera_id)} & same)

    def cmp(cid: str) -> dict[str, Any]:
        during = aggregate(_metrics(db, [cid], start, end))
        prior = aggregate(_metrics(db, [cid], *before))
        delta = {}
        if during and prior:
            for k in ("rate_per_min", "avg_speed_kmh", "occupancy", "queue"):
                if during[k] is not None and prior[k] is not None:
                    delta[k] = round(during[k] - prior[k], 3)
        return {"camera_id": cid, "during": during, "before": prior, "delta": delta}

    tt_during = segment_samples(db, start, end)
    tt_before = segment_samples(db, *before)
    segs = []
    delay_s = 0.0
    for (a, b), s in tt_during.items():
        if camera_id not in (a, b):
            continue
        prior = tt_before.get((a, b))
        med = statistics.median(s)
        bmed = statistics.median(prior) if prior else (graph.edge(a, b).typical_travel_s if graph.edge(a, b) else None)
        extra = max(0.0, med - bmed) if bmed else 0.0
        delay_s += extra * len(s)
        segs.append({"from": a, "to": b, "median_s": round(med, 1), "before_s": round(bmed, 1) if bmed else None, "samples": len(s),
                     "extra_s_per_vehicle": round(extra, 1)})
    affected = db.scalar(select(func.count(func.distinct(VehicleObservation.global_vehicle_id))).where(
        VehicleObservation.camera_id.in_([camera_id] + upstream), VehicleObservation.observed_at >= start, VehicleObservation.observed_at < end)) or 0
    alerts = db.scalars(select(Alert).where(Alert.camera_id.in_([camera_id] + upstream + downstream), Alert.created_at >= start,
                                            Alert.created_at < end).order_by(Alert.created_at)).all()
    site = cmp(camera_id)
    summary_txt = []
    d = site["delta"]
    if d.get("avg_speed_kmh") is not None:
        summary_txt.append(f"average speed changed by {d['avg_speed_kmh']:+.1f} km/h at {camera_id}")
    if d.get("queue") is not None:
        summary_txt.append(f"queue changed by {d['queue']:+.1f} vehicles")
    if delay_s:
        summary_txt.append(f"≈{delay_s / 3600:.2f} vehicle-hours of extra delay on {len(segs)} linked segment(s)")
    summary_txt.append(f"{affected} distinct vehicles passed the site or its upstream cameras during the incident")
    return {"camera_id": camera_id, "start": start.isoformat(), "end": end.isoformat(),
            "baseline_window": [before[0].isoformat(), before[1].isoformat()], "site": site,
            "upstream": [cmp(c) for c in upstream], "downstream": [cmp(c) for c in downstream], "segments": segs,
            "extra_delay_vehicle_hours": round(delay_s / 3600, 3), "vehicles_affected": affected,
            "alerts": [{"code": a.code, "type": a.type, "severity": a.severity, "created_at": a.created_at.isoformat(), "title": a.title} for a in alerts],
            "summary": summary_txt}
