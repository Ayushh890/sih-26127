"""Camera source implementations and factory."""
from __future__ import annotations

from typing import Any

from app.camera.sources.base import FAULT_KINDS, CameraSource, SourceError, safe_uri
from app.camera.sources.browser import BrowserPushSource
from app.camera.sources.demo import DemoSyntheticSource
from app.camera.sources.opencv_sources import HTTPSource, RTSPSource, VideoFileSource, WebcamSource

SOURCE_TYPES: dict[str, type[CameraSource]] = {
    "rtsp": RTSPSource,
    "http": HTTPSource,
    "webcam": WebcamSource,
    "browser": BrowserPushSource,
    "file": VideoFileSource,
    "demo": DemoSyntheticSource,
}


def create_source(camera_id: str, source_type: str, uri: str, *, username: str | None = None, password: str | None = None,
                  max_queue_size: int = 4, options: dict[str, Any] | None = None) -> CameraSource:
    cls = SOURCE_TYPES.get(source_type)
    if cls is None:
        raise SourceError(f"unsupported source type {source_type!r}", permanent=True)
    return cls(camera_id, uri, username=username, password=password, max_queue_size=max_queue_size, options=options)


def probe_source(source_type: str, uri: str, *, username: str | None = None, password: str | None = None,
                 options: dict[str, Any] | None = None, timeout: float = 8.0) -> dict[str, Any]:
    """Open a source, grab one frame and close it. Used by the "test connection" API."""
    import time

    src = create_source("probe", source_type, uri, username=username, password=password, max_queue_size=2,
                        options={**(options or {}), "open_timeout_s": timeout})
    t0 = time.perf_counter()
    try:
        src.connect()
        got = src.read_frame(timeout=timeout)
        latency = (time.perf_counter() - t0) * 1000
        if got is None:
            return {"ok": False, "error": "connected but no frame received within timeout", "latency_ms": round(latency, 1)}
        frame, _ = got
        return {"ok": True, "resolution": f"{frame.shape[1]}x{frame.shape[0]}", "native_fps": src.native_fps,
                "latency_ms": round(latency, 1), "uri": safe_uri(uri)}
    except SourceError as e:
        return {"ok": False, "error": str(e), "permanent": e.permanent, "latency_ms": round((time.perf_counter() - t0) * 1000, 1)}
    finally:
        src.stop()


__all__ = ["CameraSource", "SourceError", "FAULT_KINDS", "SOURCE_TYPES", "create_source", "probe_source", "safe_uri",
           "RTSPSource", "HTTPSource", "WebcamSource", "BrowserPushSource", "VideoFileSource", "DemoSyntheticSource"]
