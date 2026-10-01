from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _make_engine(url: str) -> Engine:
    s = get_settings()
    if url.startswith("sqlite"):
        if ":///" in url and not url.endswith(":memory:"):
            from pathlib import Path
            from urllib.parse import unquote

            raw = url.split(":///", 1)[1].split("?", 1)[0]
            db_path = Path(unquote(raw))
            # sqlite:////absolute/posix paths arrive as "/abs/..."; Windows
            # drive URLs arrive as "C:/...". A stray leading slash before a
            # drive letter ("/C:/...") breaks Path on Windows, so strip it.
            if len(str(db_path)) > 3 and str(db_path)[0] == "/" and str(db_path)[2] == ":":
                db_path = Path(str(db_path)[1:])
            if str(db_path) not in (":memory:", ""):
                db_path.parent.mkdir(parents=True, exist_ok=True)
        eng = create_engine(url, echo=s.DB_ECHO, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(eng, "connect")
        def _sqlite_pragmas(dbapi_conn, _):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return eng
    return create_engine(
        url,
        echo=s.DB_ECHO,
        pool_size=s.DB_POOL_SIZE,
        max_overflow=s.DB_POOL_SIZE,
        pool_pre_ping=True,
        pool_timeout=10,
        connect_args={"connect_timeout": 5, "options": "-c statement_timeout=15000"},
    )


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = _make_engine(get_settings().DATABASE_URL)
        _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def reset_engine(url: str | None = None) -> Engine:
    """Recreate the engine (used by tests to point at an isolated database)."""
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = _make_engine(url or get_settings().DATABASE_URL)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False, autoflush=False)
    return _engine


def SessionLocal() -> Session:  # noqa: N802 - factory used like a class
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def db_latency_ms() -> float | None:
    try:
        t0 = time.perf_counter()
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        return (time.perf_counter() - t0) * 1000
    except Exception:
        return None


def is_postgres() -> bool:
    return get_engine().dialect.name == "postgresql"


def create_schema() -> None:
    """Create all tables directly (SQLite dev/test). PostgreSQL deployments use Alembic."""
    from app.db import models  # noqa: F401 - register mappers
    from app.db.base import Base

    Base.metadata.create_all(get_engine())
