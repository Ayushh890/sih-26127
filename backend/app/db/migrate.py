"""Database initialisation: Alembic migrations on PostgreSQL, direct ``create_all`` on SQLite."""
from __future__ import annotations

from pathlib import Path

from app.core.logging import get_logger
from app.db.session import create_schema, get_engine

log = get_logger("db")

BACKEND_DIR = Path(__file__).resolve().parents[2]


def alembic_config():  # noqa: ANN201 - alembic.config.Config
    from alembic.config import Config

    from app.core.config import get_settings

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", get_settings().DATABASE_URL.replace("%", "%%"))
    return cfg


def init_database() -> str:
    """Bring the schema up to date; returns the method used."""
    if get_engine().dialect.name == "sqlite":
        create_schema()
        return "create_all"
    from alembic import command

    command.upgrade(alembic_config(), "head")
    log.info("database migrated to head")
    return "alembic"
