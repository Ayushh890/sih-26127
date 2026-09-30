"""Authentication: JWT login (rate limited, audited), current user, password change."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, can_view_raw, client_ip, get_current_user
from app.core.config import get_settings
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security import create_access_token, hash_password, verify_password
from app.db.models import User
from app.db.session import get_db
from app.schemas.requests import LoginRequest, PasswordChange
from app.services import audit

router = APIRouter(prefix="/api/auth", tags=["auth"])
login_limiter = SlidingWindowLimiter(get_settings().LOGIN_RATE_LIMIT_PER_MINUTE, 60.0)


def me_dict(db: Session, user: CurrentUser) -> dict[str, Any]:
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "role": user.role,
            "permissions": sorted(user.permissions), "can_view_raw_plates": can_view_raw(db, user)}


@router.post("/login", summary="Obtain a bearer token")
def login(body: LoginRequest, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    ip = client_ip(request) or "unknown"
    ok, _, retry = login_limiter.check(f"{ip}:{body.username.lower()}")
    if not ok:
        audit.record(db, None, "auth.login_rate_limited", "user", body.username, {}, ip, success=False)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "too many login attempts; try again later",
                            headers={"Retry-After": str(int(retry) + 1)})
    user = db.scalar(select(User).where(User.username == body.username))
    if user is None or not user.is_active or not verify_password(body.password, user.password_hash):
        # same message for unknown users and wrong passwords; the password itself is never logged
        audit.record(db, None, "auth.login_failed", "user", body.username, {"reason": "inactive" if user and not user.is_active else "bad credentials"},
                     ip, success=False)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid username or password")
    token, expires_in = create_access_token(user.username, user.role.name, {"uid": user.id})
    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    audit.record(db, {"id": user.id, "username": user.username, "role": user.role.name}, "auth.login", "user", user.username, {}, ip)
    cu = CurrentUser(id=user.id, username=user.username, role=user.role.name, full_name=user.full_name,
                     permissions=set(user.role.permissions or []))
    return {"access_token": token, "token_type": "bearer", "expires_in": expires_in, "user": me_dict(db, cu)}


@router.get("/me", summary="Current user, role and permissions")
def me(user: CurrentUser = Depends(get_current_user), db: Session = Depends(get_db)) -> dict[str, Any]:
    return me_dict(db, user)


@router.post("/password", summary="Change own password")
def change_password(body: PasswordChange, request: Request, user: CurrentUser = Depends(get_current_user),
                    db: Session = Depends(get_db)) -> dict[str, Any]:
    u = db.get(User, user.id)
    assert u is not None
    if not verify_password(body.current_password, u.password_hash):
        audit.record(db, user.as_dict(), "auth.password_change", "user", user.username, {}, client_ip(request), success=False)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "current password is incorrect")
    u.password_hash = hash_password(body.new_password)
    db.commit()
    audit.record(db, user.as_dict(), "auth.password_change", "user", user.username, {}, client_ip(request))
    return {"ok": True}
