"""Alert engine.

Alerts are produced only by explicit rules whose parameters live in the ``alerts``
settings section (each can be disabled or tuned by an administrator). Every alert
stores a plain-language reason plus structured ``details`` (the evidence and the
rule parameters that fired), is de-duplicated per ``dedup_key`` within the rule's
cooldown (repeats increment ``occurrences``) and moves NEW → ACKNOWLEDGED → RESOLVED
with the acting user and time recorded.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.bus import EventBus, ui_event
from app.core.logging import get_logger
from app.db.models import Alert, AuditLog

log = get_logger("alerts")

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
STATUSES = ("NEW", "ACKNOWLEDGED", "RESOLVED")


def alert_dict(a: Alert) -> dict[str, Any]:
    return {
        "id": a.id, "code": a.code, "type": a.type, "severity": a.severity, "status": a.status,
        "created_at": a.created_at.isoformat() if a.created_at else None, "updated_at": a.updated_at.isoformat() if a.updated_at else None,
        "camera_id": a.camera_id, "global_vehicle_id": a.global_vehicle_id, "observation_id": a.observation_id, "title": a.title,
        "reason": a.reason, "details": a.details, "confidence": a.confidence, "evidence_id": a.evidence_id, "occurrences": a.occurrences,
        "acknowledged_by": a.acknowledged_by, "acknowledged_at": a.acknowledged_at.isoformat() if a.acknowledged_at else None,
        "resolved_by": a.resolved_by, "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        "resolution_note": a.resolution_note, "is_demo": a.is_demo,
    }


def _next_code(db: Session, now: datetime) -> str:
    day = now.strftime("%Y%m%d")
    last = db.scalar(select(func.max(Alert.code)).where(Alert.code.like(f"ALR-{day}-%")))  # max, not count: retention deletes rows
    n = int(last.rsplit("-", 1)[1]) if last else 0
    return f"ALR-{day}-{n + 1:06d}"


class AlertEngine:
    def __init__(self, bus: EventBus | None = None) -> None:
        self.bus = bus

    def raise_alert(self, db: Session, cfg: dict[str, Any], *, type: str, title: str, reason: str, details: dict[str, Any],
                    dedup_key: str, severity: str | None = None, camera_id: str | None = None, global_vehicle_id: int | None = None,
                    observation_id: int | None = None, confidence: float | None = None, evidence_id: str | None = None,
                    is_demo: bool = False, when: datetime | None = None) -> Alert | None:
        rule = cfg.get(type) or {}
        if not rule.get("enabled", True):
            return None
        now = when or datetime.now(timezone.utc)
        cooldown = float(rule.get("cooldown_s", 300))
        existing = db.scalar(select(Alert).where(Alert.dedup_key == dedup_key, Alert.status != "RESOLVED",
                                                 Alert.updated_at >= now - timedelta(seconds=cooldown)).order_by(Alert.id.desc()).limit(1))
        details = {**details, "rule": {"type": type, **{k: v for k, v in rule.items() if k != "enabled"}}}
        if existing is not None:
            existing.occurrences += 1
            existing.details = {**details, "first_reason": existing.details.get("first_reason", existing.reason)}
            existing.reason = reason
            existing.updated_at = now
            db.flush()
            self._publish("alert_updated", existing)
            return existing
        # A condition that recovered on its own (auto-resolved by the system) and recurs within
        # the cooldown re-opens the same alert instead of raising a new one, so a flapping
        # camera or metric produces one alert with an occurrence count, not a stream of them.
        flapping = db.scalar(select(Alert).where(Alert.dedup_key == dedup_key, Alert.status == "RESOLVED", Alert.resolved_by == "system",
                                                 Alert.resolved_at >= now - timedelta(seconds=cooldown)).order_by(Alert.id.desc()).limit(1))
        if flapping is not None:
            reopened = int(flapping.details.get("reopened", 0)) + 1
            flapping.details = {**details, "first_reason": flapping.details.get("first_reason", flapping.reason), "reopened": reopened,
                                "last_auto_resolution": flapping.resolution_note}
            flapping.status, flapping.resolved_by, flapping.resolved_at, flapping.resolution_note = "NEW", None, None, None
            flapping.acknowledged_by = flapping.acknowledged_at = None
            flapping.occurrences += 1
            flapping.reason = reason
            flapping.updated_at = now
            db.flush()
            log.info("alert re-opened %s %s (recurred within cooldown)", flapping.code, type, extra={"camera_id": camera_id})
            self._publish("alert_updated", flapping)
            return flapping
        a = Alert(code=_next_code(db, now), type=type, severity=severity or rule.get("severity", "MEDIUM"), status="NEW", created_at=now, updated_at=now,
                  camera_id=camera_id, global_vehicle_id=global_vehicle_id, observation_id=observation_id, title=title[:255], reason=reason,
                  details=details, confidence=confidence, evidence_id=evidence_id, dedup_key=dedup_key[:128], is_demo=is_demo)
        db.add(a)
        db.flush()
        log.info("alert raised %s %s", a.code, type, extra={"camera_id": camera_id})
        self._publish("alert_created", a)
        return a

    def auto_resolve(self, db: Session, dedup_key: str, note: str) -> int:
        n = 0
        now = datetime.now(timezone.utc)
        for a in db.scalars(select(Alert).where(Alert.dedup_key == dedup_key, Alert.status != "RESOLVED")):
            a.status, a.resolved_by, a.resolved_at, a.resolution_note = "RESOLVED", "system", now, note
            n += 1
            self._publish("alert_updated", a)
        if n:
            db.flush()
        return n

    def _publish(self, kind: str, a: Alert) -> None:
        if self.bus is not None:
            self.bus.publish_ui(ui_event(kind, alert_dict(a)))


def transition(db: Session, a: Alert, action: str, user: dict[str, Any], note: str | None, ip: str | None, bus: EventBus | None) -> Alert:
    """Operator acknowledgement / resolution / re-opening (audited)."""
    now = datetime.now(timezone.utc)
    before = a.status
    if action == "acknowledge":
        if a.status != "NEW":
            raise ValueError(f"cannot acknowledge an alert in status {a.status}")
        a.status, a.acknowledged_by, a.acknowledged_at = "ACKNOWLEDGED", user["username"], now
    elif action == "resolve":
        if a.status == "RESOLVED":
            raise ValueError("alert already resolved")
        if a.acknowledged_at is None:
            a.acknowledged_by, a.acknowledged_at = user["username"], now
        a.status, a.resolved_by, a.resolved_at, a.resolution_note = "RESOLVED", user["username"], now, (note or "")[:2000]
    elif action == "reopen":
        if a.status != "RESOLVED":
            raise ValueError("only resolved alerts can be reopened")
        a.status, a.resolved_by, a.resolved_at = "NEW", None, None
    else:
        raise ValueError(f"unknown action {action}")
    a.updated_at = now
    db.add(AuditLog(user_id=user.get("id"), username=user["username"], role=user["role"], action=f"alert.{action}", resource_type="alert",
                    resource_id=a.code, details={"from": before, "to": a.status, "note": note}, ip=ip))
    db.commit()
    if bus is not None:
        bus.publish_ui(ui_event("alert_updated", alert_dict(a)))
    return a
