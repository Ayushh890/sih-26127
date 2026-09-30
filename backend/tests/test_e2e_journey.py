"""End-to-end: live cameras A -> B -> C registered through the API, observation events fed
through the real ingestion service (identity resolution, trajectory, alert rules), results
verified through the REST API exactly as the frontend consumes them."""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.services.ingestion import IngestionService
from tests.helpers import embedding, make_camera, make_edge, observation

T0 = time.time() - 1800  # half an hour ago: inside every default query window


@pytest.fixture(scope="module")
def network(client: TestClient, admin: dict[str, str]) -> dict[str, str]:
    # ~1.1 km apart along a north-south road; C is a one-way camera for the wrong-way rule
    make_camera(client, admin, "E2E-A", 26.8500, 80.9400)
    make_camera(client, admin, "E2E-B", 26.8600, 80.9400)
    make_camera(client, admin, "E2E-C", 26.8700, 80.9400, calibration={"one_way": True, "flow_toward_camera": True})
    make_edge(client, admin, "E2E-A", "E2E-B", 1100, bidirectional=True, road_name="Test Road",
              path=[[26.8500, 80.9400], [26.8550, 80.9405], [26.8600, 80.9400]])
    make_edge(client, admin, "E2E-B", "E2E-C", 1100, bidirectional=True, road_name="Test Road")
    return {"A": "E2E-A", "B": "E2E-B", "C": "E2E-C"}


@pytest.fixture(scope="module")
def ingest(bus) -> IngestionService:  # noqa: ANN001
    return IngestionService(bus, "pytest")


def feed(ingest: IngestionService, *events: dict) -> None:
    for ev in events:
        assert ingest.process(ev) is True
    assert ingest.stats["dead_lettered"] == 0, "an event was dead-lettered"


def test_topology_path(client: TestClient, admin: dict[str, str], network: dict[str, str]) -> None:
    r = client.get("/api/topology/path", params={"from": "E2E-A", "to": "E2E-C"}, headers=admin)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reachable"] is True
    assert body["cameras"] == ["E2E-A", "E2E-B", "E2E-C"]
    make_camera(client, admin, "E2E-ISO", 26.90, 80.99)
    assert client.get("/api/topology/path", params={"from": "E2E-A", "to": "E2E-ISO"}, headers=admin).json()["reachable"] is False


def test_journey_a_b_c(client: TestClient, admin: dict[str, str], analyst: dict[str, str], ingest: IngestionService, network: dict[str, str]) -> None:
    plate = "UP32TE1001"
    feed(ingest, observation("E2E-A", T0, plate), observation("E2E-B", T0 + 110, plate), observation("E2E-C", T0 + 230, plate))

    r = client.get("/api/vehicles/search", params={"plate": plate, "scope": "all"}, headers=admin)
    assert r.status_code == 200, r.text
    items = r.json()["results"]
    assert len(items) == 3
    codes = {i["vehicle_code"] for i in items}
    assert len(codes) == 1, f"the three sightings must resolve to one global vehicle, got {codes}"
    code = codes.pop()

    t = client.get(f"/api/vehicles/{code}/trajectory", headers=admin)
    assert t.status_code == 200, t.text
    traj = t.json()
    assert [p["camera_id"] for p in traj["points"]] == ["E2E-A", "E2E-B", "E2E-C"]
    assert len(traj["segments"]) == 2
    for seg in traj["segments"]:
        assert len(seg["geometry"]) >= 2
        assert seg["confidence_level"] in ("HIGH", "MEDIUM", "LOW")
        assert seg["travel_s"] == pytest.approx(110 if seg["to_camera_id"] == "E2E-B" else 120, abs=1)
    assert len(traj["segments"][0]["geometry"]) == 3  # follows the drawn road geometry, not a straight line
    for p in traj["points"][1:]:
        assert p["link_score"] is not None and p["match_reasons"], "every match carries a score and reasons"

    # the match is explainable at observation level too
    obs_id = next(i["id"] for i in items if i["camera_id"] == "E2E-C")
    o = client.get(f"/api/observations/{obs_id}", headers=admin).json()
    assert o["match_score"] is not None and o["match_confidence_level"] in ("HIGH", "MEDIUM", "LOW")
    assert o["match_components"]

    # privacy: the analyst sees a pseudonym, never the plate, and can search by pseudonym
    ta = client.get(f"/api/vehicles/{code}/trajectory", headers=analyst)
    assert ta.status_code == 200
    assert plate not in ta.text
    psn = ta.json()["vehicle"]["plate_text"]
    assert psn.startswith("PSN-")
    s = client.get("/api/vehicles/search", params={"plate": psn, "scope": "all"}, headers=analyst)
    assert s.status_code == 200 and len(s.json()["results"]) == 3 and plate not in s.text


def test_fuzzy_search_finds_ocr_confusion(client: TestClient, admin: dict[str, str], ingest: IngestionService, network: dict[str, str]) -> None:
    feed(ingest, observation("E2E-A", T0 + 400, "UP32TE2002"))
    r = client.get("/api/vehicles/search", params={"plate": "UP32TE2OO2", "fuzzy": True, "scope": "all"}, headers=admin)
    assert r.status_code == 200
    assert any(i["plate_text"] == "UP32TE2002" for i in r.json()["results"])


def test_impossible_travel_alert(client: TestClient, admin: dict[str, str], operator: dict[str, str], ingest: IngestionService,
                                 network: dict[str, str]) -> None:
    plate = "UP32TE3003"
    # 2.2 km in 15 s: physically impossible (needs >= 88 s at 25 m/s); appearance differs too
    feed(ingest, observation("E2E-A", T0 + 600, plate), observation("E2E-C", T0 + 615, plate, vehicle_class="truck", color="red"))
    alerts = client.get("/api/alerts", params={"type": "IMPOSSIBLE_TRAVEL", "scope": "all"}, headers=admin).json()["results"]
    mine = [a for a in alerts if plate in a["title"]]
    assert len(mine) == 1, alerts
    a = mine[0]
    assert a["status"] == "NEW" and "shortest allowed road path" in a["reason"]

    # the two sightings are *not* merged into one vehicle
    items = client.get("/api/vehicles/search", params={"plate": plate, "scope": "all"}, headers=admin).json()["results"]
    assert len({i["vehicle_code"] for i in items}) == 2

    # lifecycle NEW -> ACKNOWLEDGED -> RESOLVED, with an acknowledgement log
    ref = a["code"]
    r = client.post(f"/api/alerts/{ref}/acknowledge", json={"note": "checking CCTV"}, headers=operator)
    assert r.status_code == 200 and r.json()["status"] == "ACKNOWLEDGED", r.text
    assert client.post(f"/api/alerts/{ref}/acknowledge", json={}, headers=operator).status_code == 409
    r = client.post(f"/api/alerts/{ref}/resolve", json={"note": "confirmed misread"}, headers=operator)
    assert r.status_code == 200 and r.json()["status"] == "RESOLVED"
    detail = client.get(f"/api/alerts/{ref}", headers=admin).json()
    actions = [h["action"] for h in detail["history"]]
    assert "alert.acknowledge" in actions and "alert.resolve" in actions


def test_viewer_cannot_acknowledge(client: TestClient, admin: dict[str, str], viewer: dict[str, str], network: dict[str, str]) -> None:
    alerts = client.get("/api/alerts", params={"scope": "all"}, headers=admin).json()["results"]
    assert alerts
    ref = alerts[0]["code"]
    assert client.post(f"/api/alerts/{ref}/acknowledge", json={}, headers=viewer).status_code == 403


def test_wrong_way_alert(client: TestClient, admin: dict[str, str], ingest: IngestionService, network: dict[str, str]) -> None:
    feed(ingest, observation("E2E-C", T0 + 800, "UP32TE4004", motion="against_flow", frames=10))
    # a with-flow vehicle at the same camera and a wrong-way vehicle on a two-way camera raise nothing
    feed(ingest, observation("E2E-C", T0 + 830, "UP32TE4005"), observation("E2E-A", T0 + 860, "UP32TE4006", motion="against_flow"))
    alerts = client.get("/api/alerts", params={"type": "WRONG_WAY", "scope": "all"}, headers=admin).json()["results"]
    assert [a for a in alerts if "UP32TE4004" in a["title"]]
    assert not [a for a in alerts if "UP32TE4005" in a["title"] or "UP32TE4006" in a["title"]]


def test_duplicate_event_is_idempotent(client: TestClient, admin: dict[str, str], ingest: IngestionService, network: dict[str, str]) -> None:
    ev = observation("E2E-B", T0 + 1000, "UP32TE5005")
    before = ingest.stats["duplicates"]
    feed(ingest, ev, dict(ev))
    assert ingest.stats["duplicates"] == before + 1
    items = client.get("/api/vehicles/search", params={"plate": "UP32TE5005", "scope": "all"}, headers=admin).json()["results"]
    assert len(items) == 1


def test_bad_event_goes_to_dead_letter_queue(client: TestClient, admin: dict[str, str], bus) -> None:  # noqa: ANN001
    svc = IngestionService(bus, "pytest-dlq", max_attempts=2)
    assert svc.process({"type": "no_such_event", "camera_id": "E2E-A"}) is True
    assert svc.stats["dead_lettered"] == 1
    dl = client.get("/api/system/dead-letters", headers=admin).json()
    rows = dl["results"] if isinstance(dl, dict) else dl
    assert any(d["event_type"] == "no_such_event" for d in rows)

    # api-only process (ingestion runs elsewhere): replay hands the event back to the ingest stream
    dl_id = next(d["id"] for d in rows if d["event_type"] == "no_such_event" and not d["resolved"])
    r = client.post(f"/api/system/dead-letters/{dl_id}/replay", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "requeued" and r.json()["resolved"] is True
    requeued = [ev for _id, ev in bus.read_ingest("pytest", block_s=0.2)]
    assert any(ev.get("type") == "no_such_event" for ev in requeued)
    assert client.post(f"/api/system/dead-letters/{dl_id}/replay", headers=admin).status_code == 409


def test_analytics_live_scope(client: TestClient, analyst: dict[str, str], network: dict[str, str]) -> None:
    r = client.get("/api/analytics/summary", params={"scope": "live", "period": "24h"}, headers=analyst)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synthetic"] is False
    assert "UP32TE" not in r.text  # aggregates never carry plates


def test_analytics_empty_period_message(client: TestClient, analyst: dict[str, str]) -> None:
    r = client.get("/api/analytics/timeseries", params={"scope": "demo", "period": "1h"}, headers=analyst)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_data"] is False and body["message"] == "No data available for this period."


def test_od_matrix_individual_drilldown_requires_permission(client: TestClient, analyst: dict[str, str], admin: dict[str, str],
                                                            network: dict[str, str]) -> None:
    r = client.get("/api/analytics/od-matrix", params={"scope": "live", "period": "24h"}, headers=analyst)
    assert r.status_code == 200 and r.json()["drilldown_allowed"] is False
    assert client.get("/api/analytics/od-matrix/vehicles", params={"origin": "E2E-A", "destination": "E2E-C"}, headers=analyst).status_code == 403
    r = client.get("/api/analytics/od-matrix/vehicles", params={"origin": "E2E-A", "destination": "E2E-C", "scope": "live", "period": "24h"},
                   headers=admin)
    assert r.status_code == 200, r.text


def test_appearance_only_link_is_capped_and_cannot_change_plate(client: TestClient, admin: dict[str, str], ingest: IngestionService,
                                                                  network: dict[str, str]) -> None:
    emb = embedding(42)
    feed(ingest, observation("E2E-A", T0 + 1200, "UP32TE6006", embedding=emb),
         observation("E2E-B", T0 + 1310, None, embedding=emb),  # plate unreadable at B
         observation("E2E-C", T0 + 1430, "UP32TE6999", embedding=emb))  # same look, different confident plate
    first = client.get("/api/vehicles/search", params={"plate": "UP32TE6006", "scope": "all"}, headers=admin).json()["results"][0]
    traj = client.get(f"/api/vehicles/{first['vehicle_code']}/trajectory", headers=admin).json()
    assert [p["camera_id"] for p in traj["points"]] == ["E2E-A", "E2E-B"]
    b = traj["points"][1]
    assert b["confidence_level"] in ("MEDIUM", "LOW"), "a link without plate corroboration is never HIGH"
    assert any("capped" in r or "plate" not in r for r in b["match_reasons"])
    other = client.get("/api/vehicles/search", params={"plate": "UP32TE6999", "scope": "all"}, headers=admin).json()["results"][0]
    assert other["vehicle_code"] != first["vehicle_code"]
    o = client.get(f"/api/observations/{other['id']}", headers=admin).json()
    assert any("registered plate" in r or "disagree" in r for r in o["match_reasons"])


def test_congestion_and_travel_times_historical_anchor(client: TestClient, analyst: dict[str, str], network: dict[str, str]) -> None:
    until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + 600))
    r = client.get("/api/analytics/congestion", params={"scope": "live", "until": until}, headers=analyst)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["until"].startswith(until[:16]) and body["levels"][-1] == "NO_DATA"
    assert {c["camera_id"] for c in body["cameras"]} >= set(network.values())
    r = client.get("/api/analytics/travel-times", params={"scope": "live", "until": until, "window_s": 3600}, headers=analyst)
    assert r.status_code == 200 and r.json()["until"].startswith(until[:16])
    # a future anchor is clamped to now
    r = client.get("/api/analytics/congestion", params={"scope": "live", "until": "2100-01-01T00:00:00Z"}, headers=analyst)
    assert r.json()["until"] < "2100"


def test_overview_scope_and_incident_scope(client: TestClient, analyst: dict[str, str], network: dict[str, str]) -> None:
    r = client.get("/api/analytics/overview", params={"scope": "live"}, headers=analyst)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["synthetic"] is False and set(body["scopes"]) <= {"live"}
    assert "E2E-A" in {c["camera_id"] for c in body["cameras"]}
    start = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 - 60))
    r = client.post("/api/analytics/incident-impact", json={"camera_id": "E2E-B", "start": start}, headers=analyst)
    assert r.status_code == 200, r.text
    assert r.json()["scope"] == "live" and r.json()["synthetic"] is False


def test_automatic_alert_transitions_are_audited(client: TestClient, operator: dict[str, str]) -> None:
    from app.db.session import session_scope
    from app.services.alerts import AlertEngine
    from app.services.settings_service import settings_cache

    engine = AlertEngine(None)
    with session_scope() as db:
        cfg = settings_cache.section(db, "alerts")
        kw = dict(type="CAMERA_OFFLINE", title="E2E-C offline", reason="no frames", details={}, dedup_key="pytest:auto:E2E-C", camera_id="E2E-C")
        a = engine.raise_alert(db, cfg, **kw)
        code = a.code
        assert engine.auto_resolve(db, "pytest:auto:E2E-C", "camera back online") == 1
        again = engine.raise_alert(db, cfg, **kw)
        assert again.code == code and again.status == "NEW"
    r = client.get(f"/api/alerts/{code}", headers=operator)
    assert r.status_code == 200, r.text
    hist = [(h["action"], h["username"], h["details"]["to"]) for h in r.json()["history"]]
    assert ("alert.auto_resolve", "system", "RESOLVED") in hist and ("alert.auto_reopen", "system", "NEW") in hist


def test_demo_timeline_plates_are_pseudonymised_for_privacy_roles() -> None:
    from app.db.session import session_scope
    from app.services import demo_control, privacy

    with session_scope() as db:
        tl = demo_control.timeline(db)
    raw = [e["plate"] for e in tl["events"] if e["plate"]]
    assert raw, "scenario has scripted plates"
    masked = privacy.mask(tl)
    text = str(masked)
    assert not any(p in text for p in raw)
    assert all(e["plate"] is None or e["plate"].startswith("PSN-") for e in masked["events"])
