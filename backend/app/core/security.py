"""Password hashing, JWT tokens, symmetric encryption and plate pseudonymisation."""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings


# --------------------------------------------------------------------------- passwords
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72], hashed.encode())
    except ValueError:
        return False


# --------------------------------------------------------------------------- JWT
def create_access_token(subject: str, role: str, extra: dict[str, Any] | None = None) -> tuple[str, int]:
    s = get_settings()
    expires_in = s.JWT_EXPIRE_MINUTES * 60
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in)).timestamp()),
        "iss": "nirnay",
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, s.JWT_SECRET, algorithm=s.JWT_ALGORITHM), expires_in


def decode_access_token(token: str) -> dict[str, Any]:
    s = get_settings()
    return jwt.decode(token, s.JWT_SECRET, algorithms=[s.JWT_ALGORITHM], issuer="nirnay")


# --------------------------------------------------------------------------- encryption
def _fernet() -> Fernet:
    return Fernet(get_settings().fernet_key)


def encrypt_str(value: str | None) -> str | None:
    if not value:
        return None
    return _fernet().encrypt(value.encode()).decode()


def decrypt_str(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        return None


def encrypt_bytes(data: bytes) -> bytes:
    return _fernet().encrypt(data)


def decrypt_bytes(data: bytes) -> bytes:
    return _fernet().decrypt(data)


# --------------------------------------------------------------------------- pseudonyms
def plate_hash(plate: str | None) -> str | None:
    """Stable keyed hash of a normalised plate (used for privacy-preserving analytics)."""
    if not plate:
        return None
    return hmac.new(get_settings().pseudonym_key, plate.encode(), hashlib.sha256).hexdigest()


def plate_pseudonym(plate: str | None) -> str | None:
    h = plate_hash(plate)
    return f"PSN-{h[:10].upper()}" if h else None


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
