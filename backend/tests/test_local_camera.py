"""Browser Local Camera: upload WebSocket → bounded bus inbox → BrowserPushSource → the normal stream worker."""
from __future__ import annotations

import time

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.camera.sources import BrowserPushSource, SourceError, create_source
from app.camera.sources.browser import WAITING
from app.core.bus import INPUT_FRAMES_MAX, InMemoryBus
from app.ml.pipelines.anpr_pipeline import FrameOutput
from tests.test_bus import redis_bus


def jpeg(w: int = 320, h: int = 240, shade: int = 90) -> bytes:
    img = np.full((h, w, 3), shade, np.uint8)
    cv2.rectangle(img, (w // 4, h // 3), (w // 2, h // 2), (20, 180, 240), -1)
    return cv2.imencode(".jpg", img)[1].tobytes()


def token(headers: dict[str, str]) -> str:
    return headers["Authorization"].split(" ", 1)[1]


@pytest.fixture(scope="module")
def browser_cam(client: TestClient, admin: dict[str, str]) -> str:
    body = {"id": "LOCAL-T1", "name": "Laptop webcam", "latitude": 26.85, "longitude": 80.95,
            "source_type": "browser", "source_uri": "browser://local"}
    r = client.post("/api/cameras", json=body, headers=admin)
    assert r.status_code == 201, r.text
    assert r.json()["source_type"] == "browser" and r.json()["is_demo"] is False
    return "LOCAL-T1"


# --------------------------------------------------------------------------- bus inbox
@pytest.mark.parametrize("make", [InMemoryBus, redis_bus], ids=["memory", "redis"])
def test_input_inbox_is_bounded_and_keeps_newest(make) -> None:  # noqa: ANN001
    bus = make()
    for i in range(INPUT_FRAMES_MAX + 5):
        assert bus.push_input_frame("C1", b"frame-%d" % i, ts=1000.0 + i)
    got = [bus.pop_input_frame("C1", timeout=0.1) for _ in range(INPUT_FRAMES_MAX + 1)]
    kept = [g for g in got if g is not None]
    assert len(kept) == INPUT_FRAMES_MAX  # memory stays bounded however fast the browser uploads
    first = INPUT_FRAMES_MAX + 5 - INPUT_FRAMES_MAX
    assert [k[0] for k in kept] == [b"frame-%d" % i for i in range(first, INPUT_FRAMES_MAX + 5)]  # oldest dropped, FIFO
    assert kept[0][1] == pytest.approx(1000.0 + first)
    assert bus.pop_input_frame("OTHER", timeout=0.05) is None  # per-camera isolation
    bus.push_input_frame("C1", b"x")
    bus.clear_input_frames("C1")
    assert bus.pop_input_frame("C1", timeout=0.05) is None


# --------------------------------------------------------------------------- source adapter
def test_browser_source_waits_then_delivers_decoded_frames(bus: InMemoryBus) -> None:
    src = create_source("SRC-B", "browser", "browser://local", options={"open_timeout_s": 0.2})
    assert isinstance(src, BrowserPushSource) and src.max_retry_delay == 2.0
    with pytest.raises(SourceError) as e:
        src.connect()
    assert str(e.value) == WAITING and not e.value.permanent

    bus.push_input_frame("SRC-B", jpeg(320, 240))
    src.connect()
    try:
        got = src.read_frame(timeout=1.0)
        assert got is not None and got[0].shape == (240, 320, 3)
        for _ in range(3):
            bus.push_input_frame("SRC-B", jpeg(320, 240, shade=30))
        got = src.read_frame(timeout=1.0)
        assert got is not None and got[0].shape == (240, 320, 3)
        assert src.describe()["source_type"] == "browser" and src.stats.frames_in >= 2
    finally:
        src.stop()


def test_browser_source_rejects_undecodable_upload(bus: InMemoryBus) -> None:
    src = create_source("SRC-BAD", "browser", "browser://local", options={"open_timeout_s": 0.2})
    bus.push_input_frame("SRC-BAD", b"\xff\xd8 not really a jpeg")
    with pytest.raises(SourceError, match="not a decodable JPEG"):
        src.connect()
    src.stop()


class RecordingPipeline:
    """Stands in for CameraPipeline (no ONNX models in the test process): records what it is given."""

    def __init__(self) -> None:
        self.frames: list[tuple[int, ...]] = []
        self.states: dict[int, object] = {}
        self.quality = (0.0, 0.0)
        self.counters = {"vehicles": 0, "plates": 0, "ocr_conf_sum": 0.0, "ocr_reads": 0, "stationary_sum": 0.0, "occupancy_sum": 0.0,
                         "frames": 0, "vehicles_in_frame_sum": 0, "max_in_frame": 0, "speed_sum": 0.0, "speed_n": 0, "track_splits": 0}

    def process(self, frame: np.ndarray, ts: float, draw: bool = True) -> FrameOutput:
        self.frames.append(frame.shape)
        self.counters["frames"] += 1
        return FrameOutput(events=[], annotated=frame if draw else None, active_tracks=0, detections=0, occupancy=0.0,
                           stationary=0, inference_ms=1.0, stage_ms={})

    def reset_counters(self) -> dict:
        c, self.counters = self.counters, {k: 0 for k in self.counters}
        return c

    def flush(self, ts: float) -> list:
        return []


def test_stream_worker_processes_browser_frames(bus: InMemoryBus) -> None:
    """The same StreamWorker used for RTSP consumes the uploads (the real pipeline is covered in production runs)."""
    from app.workers.stream_worker import StreamWorker

    pipe = RecordingPipeline()

    class Worker(StreamWorker):
        def _new_pipeline(self):  # type: ignore[no-untyped-def]
            return pipe

    cam = {"id": "SRC-W", "name": "w", "source_type": "browser", "source_uri": "browser://local", "processing": {"processing_fps": 10},
           "calibration": {}, "is_demo": False}
    w = Worker(cam, None, bus)  # type: ignore[arg-type]
    w.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline and w.frames_processed < 3:
            bus.push_input_frame("SRC-W", jpeg(640, 360))
            time.sleep(0.1)
        assert w.frames_processed >= 3 and pipe.frames[0] == (360, 640, 3)  # decoded pixels reach the pipeline
        rt = bus.get_runtime()["SRC-W"]
        assert rt["status"] == "ONLINE" and rt["resolution"] == "640x360" and rt["frames_processed"] >= 1  # runtime is published periodically
        assert bus.get_frame("SRC-W", "raw") is not None  # the preview the console shows comes from the worker
    finally:
        w.stop()
    assert "SRC-W" not in bus.get_runtime()


# --------------------------------------------------------------------------- camera API
def test_browser_camera_uri_is_validated(client: TestClient, admin: dict[str, str]) -> None:
    body = {"id": "LOCAL-BAD", "name": "x", "latitude": 1, "longitude": 1, "source_type": "browser", "source_uri": "0"}
    r = client.post("/api/cameras", json=body, headers=admin)
    assert r.status_code == 422 and "browser://" in r.text


# --------------------------------------------------------------------------- upload websocket
def closed_with(client: TestClient, url: str) -> tuple[dict, int]:
    with client.websocket_connect(url) as ws:
        err = ws.receive_json()
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
    return err, e.value.code


def test_ingest_rejects_bad_token_role_camera_and_source(client: TestClient, browser_cam: str, admin, viewer, analyst) -> None:  # noqa: ANN001
    err, code = closed_with(client, f"/ws/cameras/{browser_cam}/ingest?token=bogus")
    assert code == 4401 and err["type"] == "error"
    for role in (viewer, analyst):  # streaming into a camera is a camera-control action
        err, code = closed_with(client, f"/ws/cameras/{browser_cam}/ingest?token={token(role)}")
        assert code == 4403 and "cameras:control" in err["data"]["detail"]
    _, code = closed_with(client, f"/ws/cameras/NOPE-99/ingest?token={token(admin)}")
    assert code == 4404
    r = client.post("/api/cameras", json={"id": "RTSP-T1", "name": "r", "latitude": 1, "longitude": 1, "source_type": "rtsp",
                                          "source_uri": "rtsp://10.0.0.1/s"}, headers=admin)
    assert r.status_code == 201
    err, code = closed_with(client, f"/ws/cameras/RTSP-T1/ingest?token={token(admin)}")
    assert code == 4400 and "not a browser" in err["data"]["detail"]


def test_ingest_acks_frames_and_fills_the_camera_inbox(client: TestClient, browser_cam: str, operator, bus: InMemoryBus) -> None:  # noqa: ANN001
    bus.clear_input_frames(browser_cam)
    with client.websocket_connect(f"/ws/cameras/{browser_cam.lower()}/ingest?token={token(operator)}") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and hello["data"]["camera_id"] == browser_cam and hello["data"]["max_in_flight"] >= 1
        frame = jpeg()
        ws.send_bytes(frame)
        ack = ws.receive_json()
        assert ack["type"] == "ack" and ack["data"]["received"] == 1 and ack["data"]["accepted"] is True
        assert "backend" in ack["data"] and ack["data"]["backend"] is None  # no worker in the test app: reported, not invented
        ws.send_bytes(b"GIF89a....")  # not a JPEG
        bad = ws.receive_json()["data"]
        assert bad["accepted"] is False and bad["rejected"] == 1 and bad["received"] == 1
        ws.send_bytes(b"\xff\xd8" + b"0" * 2_000_001)  # oversized
        assert ws.receive_json()["data"]["rejected"] == 2
        ws.send_text('{"type": "ping"}')
        assert ws.receive_json()["type"] == "pong"
    got = bus.pop_input_frame(browser_cam, timeout=0.5)
    assert got is not None and got[0] == frame


def test_second_browser_session_takes_over(client: TestClient, browser_cam: str, operator) -> None:  # noqa: ANN001
    url = f"/ws/cameras/{browser_cam}/ingest?token={token(operator)}"
    with client.websocket_connect(url) as first:
        assert first.receive_json()["type"] == "hello"
        with client.websocket_connect(url) as second:
            assert second.receive_json()["type"] == "hello"
            first.send_bytes(jpeg())  # the old tab's next upload is refused
            err = first.receive_json()
            assert err["type"] == "error" and err["data"]["code"] == 4409
            with pytest.raises(WebSocketDisconnect) as e:
                first.receive_json()
            assert e.value.code == 4409
            second.send_bytes(jpeg())
            assert second.receive_json()["data"]["received"] == 1
