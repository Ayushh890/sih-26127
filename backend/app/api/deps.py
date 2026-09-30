"""Shared API dependencies: authentication, permission checks, client IP, privacy."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.auth.permissions import Perm, permissions_for
from app.core.security import decode_access_token
from app.db.models import User
from app.db.session import get_db
from app.services import privacy
from app.services.settings_service import settings_cache

_bearer = HTTPBearer(auto_error=False)


@dataclass
class CurrentUser:
    id: int
    username: str
    role: str
    full_name: str = ""
    permissions: set[str] = field(default_factory=set)

    def has(self, perm: Perm) -> bool:
        return perm.value in self.permissions

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "username": self.username, "role": self.role}


def client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


def _user_from_token(db: Session, token: str) -> CurrentUser:
    try:
        claims = decode_access_token(token)
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or expired token", headers={"WWW-Authenticate": "Bearer"}) from None
    user = db.get(User, int(claims.get("uid", 0) or 0))
    if user is None or not user.is_active or user.username != claims.get("sub"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "account disabled or not found", headers={"WWW-Authenticate": "Bearer"})
    role = user.role.name  # the role is read from the database so demotions take effect immediately
    return CurrentUser(id=user.id, username=user.username, role=role, full_name=user.full_name,
                       permissions={p.value for p in permissions_for(role)})


def get_current_user(request: Request, creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
                     db: Session = Depends(get_db)) -> CurrentUser:
    token = creds.credentials if creds else None
    if token is None and request.url.path.endswith((".mjpg", ".jpg", "/file")):
        # <img>/<video> tags cannot send headers: media endpoints also accept ?token=
        token = request.query_params.get("token")
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    user = _user_from_token(db, token)
    request.state.username = user.username
    return user


def require(*perms: Perm):  # noqa: ANN201 - FastAPI dependency factory
    def dep(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        missing = [p.value for p in perms if not user.has(p)]
        if missing:
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"missing permission: {', '.join(missing)}")
        return user

    return dep


def can_view_raw(db: Session, user: CurrentUser) -> bool:
    return privacy.can_view_raw(user.role, settings_cache.section(db, "privacy"))


def protect(db: Session, user: CurrentUser, obj: Any) -> Any:
    """Pseudonymise plate data in a response unless the user may see raw plates."""
    return obj if can_view_raw(db, user) else privacy.mask(obj)


def not_found(what: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"{what} not found")
