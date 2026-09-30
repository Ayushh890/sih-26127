"""Data retention: deletes records older than the configured periods (``retention`` settings).

* observations (with plate reads, embeddings, trajectory points via FK cascade), tracks,
  and global vehicles with no remaining observations → ``observation_days``
* evidence files + rows → ``evidence_days`` (files are removed from disk)
* camera health, traffic metrics, system events, resolved dead letters → ``health_days``

Alerts and audit logs are kept: they are the accountability record.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, exists, select
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.models import (
    CameraHealth,
    DeadLetter,
    Evidence,
    GlobalVehicle,
    PlateRead,
    SystemEvent,
    TrafficMetric,
    TrajectoryPoint,
    VehicleEmbedding,
    VehicleObservation,
    VehicleTrack,
)
from app.evidence.store import get_evidence_store
from app.services.settings_service import settings_cache

log = get_logger("retention")


def delete_observations(db: Session, cond: Any) -> int:
    """Delete observations matching ``cond`` and their dependants (explicit, so it works without FK cascades)."""
    ids = select(VehicleObservation.id).where(cond)
    for model in (PlateRead, VehicleEmbedding, TrajectoryPoint):
        db.execute(delete(model).where(model.observation_id.in_(ids)))
    track_ids = [t for t in db.scalars(select(VehicleObservation.track_id).where(cond, VehicleObservation.track_id.is_not(None)))]
    n = db.execute(delete(VehicleObservation).where(cond)).rowcount or 0
    if track_ids:
        db.execute(delete(VehicleTrack).where(VehicleTrack.id.in_(track_ids)))
    return n


def delete_evidence(db: Session, cond: Any) -> int:
    store = get_evidence_store()
    n = 0
    for ev in db.scalars(select(Evidence).where(cond)):
        store.delete(ev.files or {})
        db.delete(ev)
        n += 1
    return n


def orphan_vehicles_cond() -> Any:
    return ~exists().where(VehicleObservation.global_vehicle_id == GlobalVehicle.id)


def run(db: Session, now: datetime | None = None) -> dict[str, int]:
    cfg = settings_cache.section(db, "retention")
    now = now or datetime.now(timezone.utc)
    obs_cut = now - timedelta(days=int(cfg["observation_days"]))
    ev_cut = now - timedelta(days=int(cfg["evidence_days"]))
    h_cut = now - timedelta(days=int(cfg["health_days"]))
    out = {
        "evidence": delete_evidence(db, Evidence.ts < ev_cut),
        "observations": delete_observations(db, VehicleObservation.observed_at < obs_cut),
    }
    out["vehicles"] = db.execute(delete(GlobalVehicle).where(GlobalVehicle.last_seen_at < obs_cut, orphan_vehicles_cond())).rowcount or 0
    out["plate_reads_orphan"] = db.execute(delete(PlateRead).where(PlateRead.observation_id.is_(None), PlateRead.ts < obs_cut)).rowcount or 0
    out["camera_health"] = db.execute(delete(CameraHealth).where(CameraHealth.ts < h_cut)).rowcount or 0
    out["traffic_metrics"] = db.execute(delete(TrafficMetric).where(TrafficMetric.bucket_start < h_cut)).rowcount or 0
    out["system_events"] = db.execute(delete(SystemEvent).where(SystemEvent.ts < h_cut)).rowcount or 0
    out["dead_letters"] = db.execute(delete(DeadLetter).where(DeadLetter.resolved.is_(True), DeadLetter.ts < h_cut)).rowcount or 0
    db.add(SystemEvent(level="INFO", source="retention", event_type="retention_run", message=f"retention removed {sum(out.values())} rows",
                       details={**out, "policy": cfg}))
    db.commit()
    log.info("retention run: %s", out)
    return out
