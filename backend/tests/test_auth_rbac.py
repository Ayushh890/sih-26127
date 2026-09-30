"""Authentication, role-based access control, privacy mode and audit."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import PASSWORD


def test_health_is_public(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_api_requires_token(client: TestClient) -> None:
    for path in ("/api/cameras", "/api/vehicles/search", "/api/alerts", "/api/analytics/summary", "/api/system/status"):
        assert client.get(path).status_code == 401, path
    assert client.get("/api/cameras", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401


def test_login_failure_does_not_leak(client: TestClient) -> None:
    r = client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})
    assert r.status_code == 401
    assert "wrong-password" not in r.text
    r = client.post("/api/auth/login", json={"username": "nobody", "password": "x"})
    assert r.status_code == 401 and r.json()["detail"] == "invalid username or password"


def test_login_rate_limited(client: TestClient) -> None:
    codes = [client.post("/api/auth/login", json={"username": "ratelimit-probe", "password": "bad"}).status_code for _ in range(15)]
    assert 429 in codes and codes[0] == 401


def test_me_reports_permissions(client: TestClient, analyst: dict[str, str], admin: dict[str, str]) -> None:
    me = client.get("/api/auth/me", headers=analyst).json()
    assert me["role"] == "analyst"
    assert "plates:view_raw" not in me["permissions"] and "analytics:read" in me["permissions"]
    assert "users:manage" in client.get("/api/auth/me", headers=admin).json()["permissions"]


def test_viewer_cannot_search_or_write(client: TestClient, viewer: dict[str, str]) -> None:
    assert client.get("/api/vehicles/search", headers=viewer).status_code == 403
    assert client.post("/api/watchlist", json={"plate": "UP32AB1234", "reason": "test"}, headers=viewer).status_code == 403
    assert client.get("/api/audit", headers=viewer).status_code == 403
    assert client.get("/api/cameras", headers=viewer).status_code == 200


def test_operator_cannot_manage_users_or_settings(client: TestClient, operator: dict[str, str]) -> None:
    assert client.get("/api/users", headers=operator).status_code == 403
    assert client.patch("/api/settings/alerts", json={"value": {"SURGE": {"enabled": False}}}, headers=operator).status_code == 403


def test_analyst_raw_plate_search_is_refused(client: TestClient, analyst: dict[str, str]) -> None:
    r = client.get("/api/vehicles/search", params={"plate": "UP32AB1234"}, headers=analyst)
    assert r.status_code == 403 and "privacy" in r.json()["detail"]


def test_search_is_audited(client: TestClient, operator: dict[str, str], admin: dict[str, str]) -> None:
    r = client.get("/api/vehicles/search", params={"plate": "ZZ99", "scope": "all"}, headers=operator)
    assert r.status_code == 200
    audit = client.get("/api/audit", params={"kind": "search"}, headers=admin).json()
    rows = audit["results"] if isinstance(audit, dict) else audit
    assert any(a["action"] == "vehicle.search" and a["username"] == "operator" for a in rows)


def test_users_admin_flow(client: TestClient, admin: dict[str, str]) -> None:
    r = client.post("/api/users", json={"username": "tmp.analyst", "password": "short", "role": "analyst"}, headers=admin)
    assert r.status_code == 422  # password policy
    r = client.post("/api/users", json={"username": "tmp.analyst", "password": "a-long-password-1", "role": "analyst"}, headers=admin)
    assert r.status_code == 201, r.text
    assert "password" not in r.text.lower() or "password_hash" not in r.text
    ok = client.post("/api/auth/login", json={"username": "tmp.analyst", "password": "a-long-password-1"})
    assert ok.status_code == 200
    assert client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


def test_validation_errors_do_not_echo_input(client: TestClient, admin: dict[str, str]) -> None:
    secret = "sup3r-s3cret-value"
    r = client.post("/api/cameras", json={"id": "X1", "password": secret, "bogus": 1}, headers=admin)
    assert r.status_code == 422
    assert secret not in r.text


def test_security_headers_and_request_id(client: TestClient) -> None:
    r = client.get("/health")
    assert r.headers.get("X-Request-ID")
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
