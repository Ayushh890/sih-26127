"""Audit trail helpers. Every privileged action and every vehicle/plate search is recorded."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog

SENSITIVE_KEYS = {"password", "new_password", "old_password", "password_encrypted", "token", "secret"}


def scrub(details: dict[str, Any] | None) -> dict[str, Any]:
    """Remove secrets from audit details (passwords are never logged)."""
    if not details:
        return {}
    out: dict[str, Any] = {}
    for k, v in details.items():
        if k.lower() in SENSITIVE_KEYS:
            out[k] = "***"
        elif isinstance(v, dict):
            out[k] = scrub(v)
        else:
            out[k] = v
    return out


def record(db: Session, user: dict[str, Any] | None, action: str, resource_type: str, resource_id: str | None = None,
           details: dict[str, Any] | None = None, ip: str | None = None, success: bool = True, commit: bool = True) -> AuditLog:
    row = AuditLog(user_id=(user or {}).get("id"), username=(user or {}).get("username", "anonymous"), role=(user or {}).get("role", "-"),
                   action=action, resource_type=resource_type, resource_id=str(resource_id) if resource_id is not None else None,
                   details=scrub(details), ip=ip, success=success)
    db.add(row)
    if commit:
        db.commit()
    return row


def audit_dict(a: AuditLog) -> dict[str, Any]:
    return {"id": a.id, "ts": a.ts.isoformat(), "user_id": a.user_id, "username": a.username, "role": a.role, "action": a.action,
            "resource_type": a.resource_type, "resource_id": a.resource_id, "details": a.details, "ip": a.ip, "success": a.success}
