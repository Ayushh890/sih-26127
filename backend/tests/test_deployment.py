"""Single-container deployment support: hosted database URLs, data-dir defaults, the demo
camera subset, the console served from the API, and the specification's endpoint aliases."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import Settings


@pytest.mark.parametrize("url", ["postgres://u:p@db:5432/n", "postgresql://u:p@db:5432/n", "postgresql+psycopg://u:p@db:5432/n"])
def test_hosted_postgres_urls_use_psycopg(url: str) -> None:
    assert Settings(DATABASE_URL=url).DATABASE_URL == "postgresql+psycopg://u:p@db:5432/n"


def test_default_sqlite_lives_in_data_dir(tmp_path: Path) -> None:
    s = Settings(DATABASE_URL="", DATA_DIR=tmp_path)
    assert s.DATABASE_URL == f"sqlite:///{(tmp_path / 'nirnay.db').as_posix()}"


def test_demo_cameras_subset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core import config
    from app.db.models import Camera
    from app.db.seed import seed_demo_network
    from app.db.session import create_schema, get_engine, reset_engine, session_scope

    original = get_engine().url.render_as_string(hide_password=False)
    monkeypatch.setenv("DEMO_CAMERAS", "CAM-01, CAM-03")
    config.get_settings.cache_clear()
    try:
        reset_engine(f"sqlite:///{(tmp_path / 'seed.db').as_posix()}")
        create_schema()
        with session_scope() as db:
            added = seed_demo_network(db, enable=True)["cameras_added"]
            db.commit()
            enabled = set(db.scalars(select(Camera.id).where(Camera.enabled.is_(True))))
        assert len(added) == 6
        assert enabled == {"CAM-01", "CAM-03"}
    finally:
        monkeypatch.delenv("DEMO_CAMERAS")
        config.get_settings.cache_clear()
        reset_engine(original)


def test_console_served_with_spa_fallback(tmp_path: Path) -> None:
    from fastapi import FastAPI

    from app.main import mount_console

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<html>console</html>")
    (tmp_path / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "favicon.svg").write_text("<svg/>")
    app = FastAPI()

    @app.get("/api/ping")
    def ping() -> dict[str, bool]:
        return {"ok": True}

    mount_console(app, tmp_path)
    c = TestClient(app)
    assert c.get("/api/ping").json() == {"ok": True}
    assert c.get("/api/unknown").status_code == 404
    assert c.get("/").text == "<html>console</html>"
    assert c.get("/vehicles/search").text == "<html>console</html>"  # client-side route
    assert c.get("/assets/app.js").text == "console.log(1)"
    assert c.get("/favicon.svg").text == "<svg/>"
    assert "root:" not in c.get("/..%2f..%2f..%2fetc%2fpasswd").text


@pytest.mark.parametrize("alias, original", [
    ("/api/analytics/traffic-volume", "/api/analytics/timeseries"),
    ("/api/analytics/od", "/api/analytics/od-matrix"),
    ("/api/analytics/travel-time", "/api/analytics/travel-times"),
    ("/api/system/metrics", "/api/system/status"),
])
def test_specification_endpoint_aliases(client: TestClient, admin: dict[str, str], alias: str, original: str) -> None:
    a, o = client.get(alias, headers=admin), client.get(original, headers=admin)
    assert a.status_code == o.status_code == 200
    assert set(a.json()) == set(o.json())
    assert client.get(alias).status_code == 401
