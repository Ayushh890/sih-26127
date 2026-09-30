"""Test fixtures: an isolated SQLite database and data directory per test session, the
in-process event bus, and an API client with the real routers (camera workers are not
started; tests feed events through the real ingestion service instead)."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="nirnay-test-"))
os.environ.update({
    "APP_ENV": "test",
    "DATABASE_URL": f"sqlite:///{_TMP / 'test.db'}",
    "DATA_DIR": str(_TMP / "data"),
    "REDIS_URL": "",
    "WORKER_MODE": "api-only",
    "SEED_DEMO": "false",
    "DEMO_USERS": "true",
    "DEMO_PASSWORD": "test-password-123",
    "LOG_JSON": "false",
    "LOG_LEVEL": "WARNING",
    "JWT_SECRET": "test-secret-" + "x" * 32,
    "RATE_LIMIT_PER_MINUTE": "100000",
})

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.bus import InMemoryBus, set_bus  # noqa: E402
from app.core.runtime import runtime  # noqa: E402
from app.db.seed import seed_all  # noqa: E402
from app.db.session import create_schema, reset_engine, session_scope  # noqa: E402

PASSWORD = "test-password-123"


@pytest.fixture(scope="session")
def bus() -> InMemoryBus:
    b = InMemoryBus()
    set_bus(b)
    runtime.bus = b
    return b


@pytest.fixture(scope="session")
def app(bus):  # noqa: ANN001, ANN201
    reset_engine()
    create_schema()
    with session_scope() as db:
        seed_all(db)
    from app.main import create_app

    return create_app(start_services=False)


@pytest.fixture(scope="session")
def client(app) -> TestClient:  # noqa: ANN001
    return TestClient(app)


def login(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="session")
def admin(client: TestClient) -> dict[str, str]:
    return login(client, "admin")


@pytest.fixture(scope="session")
def operator(client: TestClient) -> dict[str, str]:
    return login(client, "operator")


@pytest.fixture(scope="session")
def analyst(client: TestClient) -> dict[str, str]:
    return login(client, "analyst")


@pytest.fixture(scope="session")
def viewer(client: TestClient) -> dict[str, str]:
    return login(client, "viewer")
