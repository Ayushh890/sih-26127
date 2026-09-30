"""Builders for synthetic *test* inputs fed through the real ingestion path."""
from __future__ import annotations

import base64
import uuid
from typing import Any

from fastapi.testclient import TestClient


def make_camera(client: TestClient, headers: dict[str, str], cam_id: str, lat: float, lon: float, **extra: Any) -> dict[str, Any]:
    body = {"id": cam_id, "name": f"Test {cam_id}", "latitude": lat, "longitude": lon, "source_type": "rtsp",
            "source_uri": f"rtsp://192.0.2.10:554/{cam_id.lower()}", "enabled": False, "direction": "N", "road_name": "Test Road", **extra}
    r = client.post("/api/cameras", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


def make_edge(client: TestClient, headers: dict[str, str], a: str, b: str, distance_m: float, **extra: Any) -> list[dict[str, Any]]:
    r = client.post("/api/topology/edges", json={"from_camera_id": a, "to_camera_id": b, "distance_m": distance_m, **extra}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["edges"]


def observation(camera_id: str, ts: float, plate: str | None, *, conf: float = 0.97, track_id: int | None = None, vehicle_class: str = "car",
                color: str = "white", motion: str = "with_flow", frames: int = 12, speed: float | None = 38.0,
                embedding: str | None = None) -> dict[str, Any]:
    """An observation event with the exact shape emitted by ``CameraPipeline._finalize``."""
    reads = []
    if plate:
        reads = [{"raw_text": plate, "normalized_text": plate, "confidence": conf, "char_confidences": [conf] * len(plate), "is_valid": True,
                  "ts": ts - 0.2 * i, "plate_bbox": [100, 200, 180, 225], "corrections": []} for i in range(3)]
    return {
        "type": "observation", "event_id": str(uuid.uuid4()), "camera_id": camera_id, "local_track_id": track_id or int(ts * 10) % 100000,
        "is_demo": False, "first_seen": ts - 2.0, "last_seen": ts + 1.0, "observed_at": ts, "frames": frames,
        "vehicle_class": vehicle_class, "class_confidence": 0.9, "vehicle_color": color, "color_rgb": [230, 230, 230],
        "bbox": [80, 120, 320, 330], "path": [[ts - 2.0, 200, 150], [ts, 200, 250], [ts + 1.0, 200, 320]],
        "speed_kmh": speed, "motion": motion, "direction": "N" if motion == "with_flow" else "S", "heading_deg": 0.0 if motion == "with_flow" else 180.0,
        "lane": 1, "plate_text": plate, "plate_raw": plate, "plate_confidence": conf if plate else None, "plate_valid": bool(plate),
        "plate_votes": len(reads), "plate_reads": reads, "embedding": embedding, "embedding_model": "test-embedder" if embedding else None, "evidence": None,
    }


def embedding(seed: int, dim: int = 64) -> str:
    """A deterministic unit vector, base64 float32 as sent by the pipeline."""
    import numpy as np

    v = np.random.default_rng(seed).standard_normal(dim).astype(np.float32)
    return base64.b64encode((v / np.linalg.norm(v)).astype(np.float32).tobytes()).decode()
