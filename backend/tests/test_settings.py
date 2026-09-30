"""Operator-tunable settings are validated and audited."""
from __future__ import annotations

from fastapi.testclient import TestClient


def test_settings_patch_validation(client: TestClient, admin: dict[str, str]) -> None:
    assert client.patch("/api/settings/nope", json={"value": {"x": 1}}, headers=admin).status_code == 404
    assert client.patch("/api/settings/identity", json={"value": {"not_a_key": 1}}, headers=admin).status_code == 422
    ident = client.get("/api/settings/identity", headers=admin).json()
    values = ident["value"]
    num_key = next(k for k, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool))
    assert client.patch("/api/settings/identity", json={"value": {num_key: "text"}}, headers=admin).status_code == 422
    assert client.patch("/api/settings/identity", json={"value": {num_key: -1}}, headers=admin).status_code == 422
    r = client.patch("/api/settings/identity", json={"value": {num_key: values[num_key]}}, headers=admin)
    assert r.status_code == 200, r.text
    rows = client.get("/api/audit", params={"kind": "admin"}, headers=admin).json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    assert any(a["action"].startswith("settings.") for a in rows)


def test_settings_ranges_enums_and_ordering(client: TestClient, admin: dict[str, str]) -> None:
    def patch(section: str, value: dict) -> int:
        return client.patch(f"/api/settings/{section}", json={"value": value}, headers=admin).status_code

    assert patch("alerts", {"WRONG_WAY": {"severity": "URGENT"}}) == 422
    assert patch("identity", {"weights": {"plate": 1.5}}) == 422
    assert patch("identity", {"accept_threshold": 2}) == 422
    assert patch("congestion", {"thresholds": {"moderate": 80}}) == 422  # above heavy/severe
    assert patch("congestion", {"thresholds": {"severe": 140}}) == 422
    assert patch("identity", {"medium_confidence": 0.9}) == 422  # above high_confidence
    assert patch("alerts", {"WRONG_WAY": {"severity": "CRITICAL"}}) == 200
    assert client.delete("/api/settings/alerts", headers=admin).status_code == 200


def test_flapping_condition_reopens_alert_within_cooldown(client) -> None:  # noqa: ANN001
    from app.db.session import session_scope
    from app.services.alerts import AlertEngine
    from app.services.settings_service import settings_cache

    eng = AlertEngine()
    key = "test:flap:CAM-X"
    with session_scope() as db:
        cfg = settings_cache.section(db, "alerts")
        a = eng.raise_alert(db, cfg, type="CAMERA_DEGRADED", title="t", reason="first", details={}, dedup_key=key)
        first = a.code
        assert eng.auto_resolve(db, key, "recovered") == 1
        b = eng.raise_alert(db, cfg, type="CAMERA_DEGRADED", title="t", reason="again", details={}, dedup_key=key)
        assert b.code == first and b.status == "NEW" and b.occurrences == 2 and b.details["reopened"] == 1
        assert b.details["first_reason"] == "first" and b.resolved_at is None
