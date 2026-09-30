"""Camera registry: credential handling and input validation."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.helpers import make_camera


def test_rtsp_credentials_never_returned(client: TestClient, admin: dict[str, str], analyst: dict[str, str]) -> None:
    cam = make_camera(client, admin, "CRED-1", 26.85, 80.95, source_uri="rtsp://opsuser:Pa55word!@192.0.2.20:554/stream1")
    assert cam["source_uri"] == "rtsp://192.0.2.20:554/stream1"
    assert cam["has_credentials"] is True
    assert cam["username_hint"].startswith("op") and "opsuser" not in cam["username_hint"]
    for headers in (admin, analyst):
        for body in (client.get("/api/cameras/CRED-1", headers=headers).text, client.get("/api/cameras", params={"scope": "all"}, headers=headers).text):
            assert "Pa55word" not in body and "opsuser" not in body


def test_camera_validation(client: TestClient, admin: dict[str, str]) -> None:
    base = {"id": "BAD-1", "name": "bad", "latitude": 26.8, "longitude": 80.9}
    assert client.post("/api/cameras", json={**base, "source_type": "rtsp", "source_uri": "http://x/y"}, headers=admin).status_code == 422
    assert client.post("/api/cameras", json={**base, "source_type": "rtsp", "source_uri": "rtsp://h/s", "extra": 1}, headers=admin).status_code == 422
    assert client.post("/api/cameras", json={**base, "latitude": 123, "source_type": "rtsp", "source_uri": "rtsp://h/s"}, headers=admin).status_code == 422
    assert client.post("/api/cameras", json={**base, "source_type": "demo", "source_uri": "demo://x"}, headers=admin).status_code == 400
    assert client.post("/api/cameras", json={**base, "source_type": "file", "source_uri": "/nonexistent/video.mp4"}, headers=admin).status_code == 400
    r = client.post("/api/cameras", json={**base, "source_type": "webcam", "source_uri": "0", "processing": {"processing_fps": 99}}, headers=admin)
    assert r.status_code == 422


def test_camera_update_and_delete_are_audited(client: TestClient, admin: dict[str, str]) -> None:
    make_camera(client, admin, "UPD-1", 26.86, 80.96)
    r = client.patch("/api/cameras/UPD-1", json={"name": "Renamed", "processing": {"processing_fps": 4, "frame_skip": 1, "max_queue_size": 8}}, headers=admin)
    assert r.status_code == 200 and r.json()["name"] == "Renamed"
    assert r.json()["processing"]["frame_skip"] == 1
    assert client.delete("/api/cameras/UPD-1", headers=admin).status_code in (200, 204)
    assert client.get("/api/cameras/UPD-1", headers=admin).status_code == 404
    rows = client.get("/api/audit", params={"kind": "admin"}, headers=admin).json()
    rows = rows["results"] if isinstance(rows, dict) else rows
    actions = {a["action"] for a in rows}
    assert {"camera.create", "camera.update", "camera.delete"} <= actions


def test_viewer_cannot_create_camera(client: TestClient, viewer: dict[str, str]) -> None:
    r = client.post("/api/cameras", json={"id": "V-1", "name": "v", "latitude": 1, "longitude": 1, "source_type": "webcam", "source_uri": "0"},
                    headers=viewer)
    assert r.status_code == 403


def test_edge_update_direction_and_geometry(client: TestClient, admin: dict[str, str]) -> None:
    from tests.helpers import make_camera, make_edge

    make_camera(client, admin, "EDG-A", 26.80, 80.90)
    make_camera(client, admin, "EDG-B", 26.81, 80.90)
    edge = make_edge(client, admin, "EDG-A", "EDG-B", 1100)[0]
    assert edge["has_geometry"] is False
    path = [[26.80, 80.90], [26.805, 80.901], [26.81, 80.90]]
    r = client.patch(f"/api/topology/edges/{edge['id']}", json={"direction": "N", "path": path}, headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["direction"] == "N" and r.json()["has_geometry"] is True
    feat = next(f for f in client.get("/api/topology", headers=admin).json()["geojson"]["features"]
                if f["properties"]["from"] == "EDG-A" and f["properties"]["to"] == "EDG-B")
    assert feat["geometry"]["coordinates"][1] == [80.901, 26.805]  # stored as GeoJSON [lon, lat]
    assert client.patch(f"/api/topology/edges/{edge['id']}", json={"path": [[26.8, 80.9]]}, headers=admin).status_code == 422
    r = client.patch(f"/api/topology/edges/{edge['id']}", json={"path": []}, headers=admin)
    assert r.status_code == 200 and r.json()["has_geometry"] is False
