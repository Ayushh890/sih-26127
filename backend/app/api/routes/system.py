"""System status, dead-letter queue, system events, audit log and runtime settings."""
from __future__ import annotations

import platform
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, require
from app.auth.permissions import Perm
from app.core.config import effective_cpu_count, get_settings
from app.core.runtime import runtime
from app.db.models import (Alert, AuditLog, Camera, DeadLetter, Evidence, GlobalVehicle, SystemEvent, SystemSetting, VehicleObservation,
                           WorkerHeartbeat)
from app.db.session import db_latency_ms, get_db, is_postgres
from app.evidence.store import get_evidence_store
from app.schemas.requests import SettingsUpdate
from app.services import audit, retention
from app.services.audit import audit_dict
from app.services.settings_service import SECTIONS, _deep_merge, check_consistency, reset_section, settings_cache, update_section, validate_patch

router = APIRouter(prefix="/api", tags=["system"])

HEARTBEAT_STALE_S = 35
APP_VERSION = "1.0.0"


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/system/status", summary="Workers, pipeline, bus, database, models and storage")
def system_status(user: CurrentUser = Depends(require(Perm.SYSTEM_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    s = get_settings()
    now = datetime.now(timezone.utc)
    workers = []
    for hb in db.scalars(select(WorkerHeartbeat).order_by(WorkerHeartbeat.worker_id)):
        age = (now - _utc(hb.ts)).total_seconds()
        workers.append({"worker_id": hb.worker_id, "last_heartbeat": hb.ts.isoformat(), "age_s": round(age, 1),
                        "alive": age < HEARTBEAT_STALE_S, "info": hb.info})
    bus = runtime.bus
    bus_ok = bool(bus and bus.ping())
    models = runtime.manager.registry.status() if runtime.manager and runtime.manager.registry else next(
        ((w["info"] or {}).get("models") for w in workers if w["alive"]), [])
    cams = dict(db.execute(select(Camera.status, func.count()).group_by(Camera.status)).all())
    dl_open = db.scalar(select(func.count()).select_from(DeadLetter).where(DeadLetter.resolved.is_(False))) or 0
    store = get_evidence_store()
    since = now - timedelta(hours=1)
    return {
        "app": {"name": "NIRNAY", "env": s.APP_ENV, "version": APP_VERSION, "python": platform.python_version()},
        "runtime": runtime.describe(),
        "hardware": {"effective_cpus": effective_cpu_count(), "inference_threads": s.inference_threads, "inference_device": s.INFERENCE_DEVICE,
                     "demo_processing_fps": s.demo_processing_fps},
        "database": {"backend": "postgresql" if is_postgres() else "sqlite", "latency_ms": db_latency_ms()},
        "bus": {"backend": bus.backend if bus else None, "reachable": bus_ok, "ingest_depth": bus.ingest_depth() if bus_ok else None,
                "publish_failures": getattr(bus, "publish_failures", 0) if bus else None},
        "ingestion": runtime.ingestion.stats if runtime.ingestion else None,
        "scheduler": runtime.scheduler.status() if runtime.scheduler else None,
        "workers": workers,
        "models": models,
        "cameras": {"by_status": cams, "total": sum(cams.values())},
        "dead_letters_open": dl_open,
        "evidence": {"usage_mb": round(store.usage_bytes() / 1e6, 1), "limit_mb": round(store.max_bytes / 1e6, 1),
                     "encrypted": store.encrypt, "files": db.scalar(select(func.count()).select_from(Evidence)) or 0},
        "throughput_1h": {"observations": db.scalar(select(func.count()).select_from(VehicleObservation).where(VehicleObservation.observed_at >= since)) or 0,
                          "vehicles": db.scalar(select(func.count()).select_from(GlobalVehicle).where(GlobalVehicle.last_seen_at >= since)) or 0,
                          "alerts": db.scalar(select(func.count()).select_from(Alert).where(Alert.created_at >= since)) or 0},
        "privacy": {"privacy_mode": s.PRIVACY_MODE, "pseudonymised_roles": settings_cache.section(db, "privacy").get("pseudonymise_for_roles", [])},
    }


@router.get("/system/events", summary="System event log (camera status changes, failures, restarts)")
def system_events(level: str | None = Query(None, pattern="^(DEBUG|INFO|WARNING|ERROR|CRITICAL)$"), camera_id: str | None = Query(None, max_length=32),
                  source: str | None = Query(None, max_length=32), limit: int = Query(100, ge=1, le=1000),
                  user: CurrentUser = Depends(require(Perm.SYSTEM_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(SystemEvent)
    if level:
        q = q.where(SystemEvent.level == level)
    if camera_id:
        q = q.where(SystemEvent.camera_id == camera_id)
    if source:
        q = q.where(SystemEvent.source == source)
    rows = db.scalars(q.order_by(SystemEvent.ts.desc()).limit(limit))
    return {"results": [{"id": e.id, "ts": e.ts.isoformat(), "level": e.level, "source": e.source, "camera_id": e.camera_id,
                         "event_type": e.event_type, "message": e.message, "details": e.details} for e in rows]}


# ---------------------------------------------------------------------- dead letters
def _dl_dict(d: DeadLetter, full: bool = False) -> dict[str, Any]:
    out = {"id": d.id, "ts": d.ts.isoformat(), "source": d.source, "event_type": d.event_type, "camera_id": d.camera_id, "error": d.error,
           "attempts": d.attempts, "resolved": d.resolved, "resolved_at": d.resolved_at.isoformat() if d.resolved_at else None,
           "resolution": d.resolution}
    if full:
        out["payload_keys"] = sorted((d.payload or {}).keys())
    return out


@router.get("/system/dead-letters", summary="Events that failed ingestion after retries")
def dead_letters(resolved: bool = False, limit: int = Query(100, ge=1, le=500), user: CurrentUser = Depends(require(Perm.SYSTEM_READ)),
                 db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(select(DeadLetter).where(DeadLetter.resolved.is_(resolved)).order_by(DeadLetter.ts.desc()).limit(limit))
    return {"results": [_dl_dict(d, True) for d in rows]}


@router.post("/system/dead-letters/{dl_id}/replay", summary="Retry a dead-lettered event")
def replay(dl_id: int, request: Request, user: CurrentUser = Depends(require(Perm.SETTINGS_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    d = db.get(DeadLetter, dl_id)
    if d is None:
        raise not_found("dead letter")
    if d.resolved:
        raise HTTPException(status.HTTP_409_CONFLICT, "already resolved")
    if runtime.ingestion is not None:
        ok = runtime.ingestion.replay_dead_letter(db, d)
        mode = "in-process"
    else:
        # ingestion runs in a worker process: hand the event back to the durable ingest stream;
        # if it fails again there it is dead-lettered again as a new entry
        if runtime.bus is None or not runtime.bus.publish_ingest(dict(d.payload)):
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "event bus unavailable; cannot requeue the event")
        d.resolved, d.resolved_at, d.resolution = True, datetime.now(timezone.utc), f"requeued to ingest stream by {user.username}"
        ok, mode = True, "requeued"
    audit.record(db, user.as_dict(), "dead_letter.replay", "dead_letter", str(dl_id), {"ok": ok, "mode": mode}, client_ip(request))
    return {"replayed": ok, "mode": mode, **_dl_dict(d)}


@router.post("/system/dead-letters/{dl_id}/discard", summary="Mark a dead-lettered event as discarded")
def discard(dl_id: int, request: Request, user: CurrentUser = Depends(require(Perm.SETTINGS_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    d = db.get(DeadLetter, dl_id)
    if d is None:
        raise not_found("dead letter")
    d.resolved, d.resolved_at, d.resolution = True, datetime.now(timezone.utc), f"discarded by {user.username}"
    audit.record(db, user.as_dict(), "dead_letter.discard", "dead_letter", str(dl_id), None, client_ip(request))
    return _dl_dict(d)


@router.post("/system/retention/run", summary="Apply data/evidence retention now")
def run_retention(request: Request, user: CurrentUser = Depends(require(Perm.SETTINGS_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    out = retention.run(db)
    audit.record(db, user.as_dict(), "retention.run", "system", None, out, client_ip(request))
    return {"deleted": out, "policy": settings_cache.section(db, "retention")}


# ---------------------------------------------------------------------- audit
@router.get("/audit", summary="Audit log (search history, evidence access, alert acknowledgements, admin actions)")
def audit_log(action: str | None = Query(None, max_length=64), username: str | None = Query(None, max_length=64),
              resource_type: str | None = Query(None, max_length=32), kind: str | None = Query(None, pattern="^(search|alert|evidence|auth|admin)$"),
              since: datetime | None = None, until: datetime | None = None, limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0),
              user: CurrentUser = Depends(require(Perm.AUDIT_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    q = select(AuditLog)
    prefixes = {"search": ("vehicle.search", "watchlist.retrospective", "analytics.od_drilldown", "vehicle.trajectory_view", "observation.view"),
                "alert": ("alert.",), "evidence": ("evidence.",), "auth": ("auth.",),
                "admin": ("user.", "camera.", "settings.", "topology.", "watchlist.", "retention.", "dead_letter.", "demo.")}
    if kind:
        q = q.where(or_(*[AuditLog.action.like(p + "%") for p in prefixes[kind]]))
    if action:
        q = q.where(AuditLog.action == action)
    if username:
        q = q.where(AuditLog.username == username)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if since:
        q = q.where(AuditLog.ts >= since)
    if until:
        q = q.where(AuditLog.ts <= until)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit).offset(offset))
    return {"total": total, "results": [audit_dict(a) for a in rows]}


# ---------------------------------------------------------------------- settings
@router.get("/settings", summary="All runtime settings sections (identity, ocr, congestion, alerts, retention, privacy)")
def get_settings_all(user: CurrentUser = Depends(require(Perm.SYSTEM_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    meta = {r.key: {"updated_by": r.updated_by, "updated_at": r.updated_at.isoformat() if r.updated_at else None}
            for r in db.scalars(select(SystemSetting))}
    return {"sections": settings_cache.all(db), "customised": meta}


@router.get("/settings/{section}", summary="One settings section")
def get_section(section: str, user: CurrentUser = Depends(require(Perm.SYSTEM_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    if section not in SECTIONS:
        raise not_found("settings section")
    return {"section": section, "value": settings_cache.section(db, section)}


@router.patch("/settings/{section}", summary="Update a settings section (deep-merged, validated, audited)")
def patch_section(section: str, body: SettingsUpdate, request: Request, user: CurrentUser = Depends(require(Perm.SETTINGS_WRITE)),
                  db: Session = Depends(get_db)) -> dict[str, Any]:
    try:
        validate_patch(section, body.value)
        before = settings_cache.section(db, section)
        check_consistency(section, _deep_merge(before, body.value))
    except KeyError:
        raise not_found("settings section") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    value = update_section(db, section, body.value, user.username)
    audit.record(db, user.as_dict(), "settings.update", "settings", section, {"patch": body.value, "before": before}, client_ip(request))
    return {"section": section, "value": value}


@router.delete("/settings/{section}", summary="Reset a settings section to defaults (audited)")
def reset(section: str, request: Request, user: CurrentUser = Depends(require(Perm.SETTINGS_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    if section not in SECTIONS:
        raise not_found("settings section")
    value = reset_section(db, section)
    audit.record(db, user.as_dict(), "settings.reset", "settings", section, None, client_ip(request))
    return {"section": section, "value": value}
