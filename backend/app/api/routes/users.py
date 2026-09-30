"""User and role administration (admin only, audited)."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, require
from app.auth.permissions import ROLE_DESCRIPTIONS, ROLE_PERMISSIONS, Perm
from app.core.security import hash_password
from app.db.models import Role, User
from app.db.session import get_db
from app.schemas.requests import UserCreate, UserUpdate
from app.services import audit

router = APIRouter(prefix="/api/users", tags=["users"])


def user_dict(u: User) -> dict[str, Any]:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "role": u.role.name, "is_active": u.is_active, "is_demo": u.is_demo,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


@router.get("/roles", summary="Roles and their permissions")
def roles(_: CurrentUser = Depends(require(Perm.USERS_MANAGE))) -> list[dict[str, Any]]:
    return [{"name": r, "description": ROLE_DESCRIPTIONS[r], "permissions": sorted(p.value for p in perms)} for r, perms in ROLE_PERMISSIONS.items()]


@router.get("", summary="List users")
def list_users(_: CurrentUser = Depends(require(Perm.USERS_MANAGE)), db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return [user_dict(u) for u in db.scalars(select(User).order_by(User.username))]


def _role(db: Session, name: str) -> Role:
    r = db.scalar(select(Role).where(Role.name == name))
    if r is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown role {name}")
    return r


@router.post("", status_code=201, summary="Create a user")
def create_user(body: UserCreate, request: Request, user: CurrentUser = Depends(require(Perm.USERS_MANAGE)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    if db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status.HTTP_409_CONFLICT, "username already exists")
    u = User(username=body.username, full_name=body.full_name, password_hash=hash_password(body.password), role_id=_role(db, body.role).id)
    db.add(u)
    db.commit()
    db.refresh(u)
    audit.record(db, user.as_dict(), "user.create", "user", u.username, {"role": body.role}, client_ip(request))
    return user_dict(u)


@router.patch("/{user_id}", summary="Update a user (role, active flag, password reset)")
def update_user(user_id: int, body: UserUpdate, request: Request, user: CurrentUser = Depends(require(Perm.USERS_MANAGE)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    u = db.get(User, user_id)
    if u is None:
        raise not_found("user")
    changes: dict[str, Any] = {}
    if u.id == user.id and (body.is_active is False or (body.role and body.role != "admin")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "you cannot deactivate or demote your own account")
    if body.full_name is not None:
        u.full_name = changes["full_name"] = body.full_name
    if body.role is not None:
        u.role_id = _role(db, body.role).id
        changes["role"] = body.role
    if body.is_active is not None:
        u.is_active = changes["is_active"] = body.is_active
    if body.password:
        u.password_hash = hash_password(body.password)
        changes["password"] = "reset"
    db.commit()
    db.refresh(u)
    audit.record(db, user.as_dict(), "user.update", "user", u.username, changes, client_ip(request))
    return user_dict(u)
