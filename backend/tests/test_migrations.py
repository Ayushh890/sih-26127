"""The Alembic migration creates exactly the schema of the ORM models and is reversible."""
from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command

from app.db.base import Base
from app.db.migrate import alembic_config


def _cfg(url: str):  # noqa: ANN202
    cfg = alembic_config()
    cfg.set_main_option("sqlalchemy.url", url)
    cfg.attributes["configure_logger"] = False
    return cfg


def test_upgrade_matches_models_and_downgrades(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'migrate.db'}"
    cfg = _cfg(url)
    command.upgrade(cfg, "head")
    command.check(cfg)  # raises if the models and the migrated schema differ
    tables = set(sa.inspect(sa.create_engine(url)).get_table_names())
    assert set(Base.metadata.tables) <= tables
    command.downgrade(cfg, "base")
    assert set(sa.inspect(sa.create_engine(url)).get_table_names()) <= {"alembic_version"}


def test_postgres_sql_includes_postgis(capsys) -> None:  # noqa: ANN001
    command.upgrade(_cfg("postgresql+psycopg://user:pw@localhost/nirnay"), "head", sql=True)
    sql = capsys.readouterr().out
    assert "CREATE EXTENSION IF NOT EXISTS postgis" in sql
    assert "USING GIST (geom)" in sql and "JSONB" in sql
