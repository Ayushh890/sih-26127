"""Alerts (produced only by the explicit, configurable rules in ``alert_rules``) and
their operator workflow NEW → ACKNOWLEDGED → RESOLVED. Every transition is audited."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, protect, require
from app.auth.permissions import Perm
from app.core.runtime import runtime
from app.db.models import Alert, AuditLog, Evidence, GlobalVehicle
from app.db.session import get_db
from app.schemas.requests import AlertAction
from app.services import alerts as alert_service
from app.services.audit import audit_dict
from app.services.scope import apply_scope, resolve_scope, scope_info
from app.services.settings_service import settings_cache
from app.services.views import evidence_dict

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _get(db: Session, ref: str) -> Alert:
    a = db.get(Alert, int(ref)) if ref.isdigit() else db.scalar(select(Alert).where(Alert.code == ref.upper()))
    if a is None:
        raise not_found("alert")
    return a


@router.get("", summary="List alerts")
def list_alerts(status_: str | None = Query(None, alias="status", pattern="^(NEW|ACKNOWLEDGED|RESOLVED|OPEN)$"),
                severity: str | None = Query(None, pattern="^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$"), type: str | None = Query(None, max_length=32),
                camera_id: str | None = Query(None, max_length=32), since: datetime | None = None, scope: str | None = Query(None, pattern="^(live|demo|all)$"),
                limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                user: CurrentUser = Depends(require(Perm.ALERTS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    sc = resolve_scope(db, scope)
    q = apply_scope(select(Alert), Alert.is_demo, sc)
    if status_ == "OPEN":
        q = q.where(Alert.status != "RESOLVED")
    elif status_:
        q = q.where(Alert.status == status_)
    if severity:
        q = q.where(Alert.severity == severity)
    if type:
        q = q.where(Alert.type == type.upper())
    if camera_id:
        q = q.where(Alert.camera_id == camera_id)
    if since:
        q = q.where(Alert.created_at >= since)
    total = db.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = db.scalars(q.order_by(Alert.created_at.desc()).limit(limit).offset(offset))
    counts_q = apply_scope(select(Alert.status, func.count()), Alert.is_demo, sc).where(
        Alert.created_at >= datetime.now(timezone.utc) - timedelta(days=7)).group_by(Alert.status)
    return protect(db, user, {**scope_info(sc), "total": total, "counts_7d": dict(db.execute(counts_q).all()),
                              "results": [alert_service.alert_dict(a) for a in rows]})


@router.get("/rules", summary="Configured alert rules (edit via /api/settings/alerts)")
def rules(user: CurrentUser = Depends(require(Perm.ALERTS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    return {"rules": settings_cache.section(db, "alerts"),
            "note": "Alerts are only raised by these explicit rules. The watchlist starts empty; entries are added by authorised users."}


@router.get("/{ref}", summary="Alert detail with explanation, evidence and acknowledgement log")
def detail(ref: str, user: CurrentUser = Depends(require(Perm.ALERTS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    a = _get(db, ref)
    d = alert_service.alert_dict(a)
    v = db.get(GlobalVehicle, a.global_vehicle_id) if a.global_vehicle_id else None
    d["vehicle_code"] = v.code if v else None
    ev = db.get(Evidence, a.evidence_id) if a.evidence_id and user.has(Perm.EVIDENCE_READ) else None
    d["evidence"] = evidence_dict(ev) if ev else None
    d["history"] = [audit_dict(x) for x in db.scalars(select(AuditLog).where(AuditLog.resource_type == "alert", AuditLog.resource_id == a.code)
                                                        .order_by(AuditLog.ts))]
    return protect(db, user, d)


def _act(action: str):  # noqa: ANN202
    def handler(ref: str, request: Request, body: AlertAction | None = None, user: CurrentUser = Depends(require(Perm.ALERTS_ACT)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
        a = _get(db, ref)
        try:
            alert_service.transition(db, a, action, user.as_dict(), body.note if body else None, client_ip(request), runtime.bus)
        except ValueError as exc:
            raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        return protect(db, user, alert_service.alert_dict(a))
    handler.__name__ = f"alert_{action}"
    return handler


for _action in ("acknowledge", "resolve", "reopen"):
    router.post(f"/{{ref}}/{_action}", summary=f"{_action.capitalize()} an alert (audited)")(_act(_action))
