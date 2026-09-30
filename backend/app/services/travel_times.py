"""Observed segment travel times between cameras (median of linked trajectory segments)."""
from __future__ import annotations

import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import TrajectoryPoint
from app.services.topology import TopologyGraph

_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def segment_samples(db: Session, since: datetime, until: datetime | None = None, min_level: tuple[str, ...] = ("HIGH", "MEDIUM")) -> dict[tuple[str, str], list[float]]:
    q = select(TrajectoryPoint.from_camera_id, TrajectoryPoint.camera_id, TrajectoryPoint.segment_travel_s).where(
        TrajectoryPoint.ts >= since, TrajectoryPoint.from_camera_id.is_not(None), TrajectoryPoint.segment_travel_s.is_not(None),
        TrajectoryPoint.confidence_level.in_(min_level))
    if until is not None:
        q = q.where(TrajectoryPoint.ts < until)
    out: dict[tuple[str, str], list[float]] = {}
    for a, b, s in db.execute(q):
        if a != b:
            out.setdefault((a, b), []).append(float(s))
    return out


def baselines(db: Session, graph: TopologyGraph, baseline_s: float = 6 * 3600, exclude_recent_s: float = 900, min_samples: int = 5,
              now: datetime | None = None) -> dict[tuple[str, str], dict]:
    """Per-edge baseline travel time: observed median when enough history exists, else the topology prior.

    History is ``[now − baseline_s, now − exclude_recent_s)``; ``now`` defaults to the current
    time (cached for 30 s) and can be set to evaluate a past period."""
    live = now is None
    key = f"{baseline_s}:{exclude_recent_s}:{min_samples}"
    if live:
        with _lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < 30:
                return hit[1]
    now = now or datetime.now(timezone.utc)
    samples = segment_samples(db, now - timedelta(seconds=baseline_s), now - timedelta(seconds=exclude_recent_s))
    out = {}
    for e in graph.edges:
        if not e.allowed:
            continue
        s = samples.get((e.from_id, e.to_id), [])
        if len(s) >= min_samples:
            out[(e.from_id, e.to_id)] = {"seconds": statistics.median(s), "source": "observed", "samples": len(s)}
        else:
            out[(e.from_id, e.to_id)] = {"seconds": e.typical_travel_s, "source": "topology_prior", "samples": len(s)}
    if live:
        with _lock:
            _cache[key] = (time.time(), out)
    return out


def path_baseline_s(base: dict[tuple[str, str], dict], cams: list[str], fallback: float) -> float:
    total = 0.0
    for a, b in zip(cams, cams[1:]):
        v = base.get((a, b))
        if v is None:
            return fallback
        total += v["seconds"]
    return total or fallback
