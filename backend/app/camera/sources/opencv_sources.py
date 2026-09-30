"""OpenCV/FFmpeg-backed sources: RTSP, HTTP (MJPEG stream or JPEG snapshot), webcam, video file."""
from __future__ import annotations

import os
import time
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

import cv2
import httpx
import numpy as np

from app.camera.sources.base import CameraSource, SourceError
from app.core.config import get_settings


def inject_credentials(uri: str, username: str | None, password: str | None) -> str:
    """Return ``uri`` with credentials in the netloc (used only inside the worker process)."""
    if not username:
        return uri
    parts = urlsplit(uri)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    cred = quote(username, safe="")
    if password:
        cred += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{cred}@{host}", parts.path, parts.query, parts.fragment))


class _CaptureSource(CameraSource):
    """Shared VideoCapture handling."""

    def __init__(self, *a, **kw) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*a, **kw)
        self.cap: cv2.VideoCapture | None = None

    def _capture_target(self) -> str | int:
        return self.uri

    def _open(self) -> None:
        timeout_ms = int(self.options.get("open_timeout_s", get_settings().CAMERA_TIMEOUT) * 1000)
        params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, timeout_ms]
        target = self._capture_target()
        cap = cv2.VideoCapture(target, self._backend(), params) if not isinstance(target, int) else cv2.VideoCapture(target)
        if not cap.isOpened():
            cap.release()
            raise SourceError(self._open_error())
        ok, frame = cap.read()
        if not ok or frame is None:
            cap.release()
            raise SourceError("stream opened but returned no frames")
        fps = cap.get(cv2.CAP_PROP_FPS)
        self.native_fps = round(fps, 2) if fps and 0 < fps < 240 else None
        self.resolution = (int(frame.shape[1]), int(frame.shape[0]))
        self.cap = cap
        self._first = frame

    def _backend(self) -> int:
        return cv2.CAP_FFMPEG

    def _open_error(self) -> str:
        return "could not open stream (host unreachable, wrong path or authentication failed)"

    def _grab(self) -> np.ndarray | None:
        first = getattr(self, "_first", None)
        if first is not None:
            self._first = None
            return first
        if self.cap is None:
            return None
        ok, frame = self.cap.read()
        return frame if ok else None

    def _close(self) -> None:
        if self.cap is not None:
            self.cap.release()
            self.cap = None


class RTSPSource(_CaptureSource):
    source_type = "rtsp"

    def _capture_target(self) -> str:
        transport = self.options.get("transport") or self.options.get("rtsp_transport") or "tcp"
        # FFmpeg reads capture options from the environment at open time
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = f"rtsp_transport;{transport}|stimeout;{int(get_settings().CAMERA_TIMEOUT * 1e6)}"
        return inject_credentials(self.uri, self._username, self._password)

    def _open(self) -> None:
        if not self.uri.lower().startswith(("rtsp://", "rtsps://")):
            raise SourceError("RTSP URI must start with rtsp://", permanent=True)
        super()._open()


class HTTPSource(_CaptureSource):
    """HTTP MJPEG stream (``mode=mjpeg``, default) or periodically polled JPEG snapshot URL."""

    source_type = "http"

    def _open(self) -> None:
        if not self.uri.lower().startswith(("http://", "https://")):
            raise SourceError("HTTP URI must start with http:// or https://", permanent=True)
        if self.options.get("mode") == "snapshot":
            self.client = httpx.Client(timeout=get_settings().CAMERA_TIMEOUT, auth=(self._username, self._password or "") if self._username else None)
            frame = self._snapshot()
            if frame is None:
                raise SourceError(self.last_error or "snapshot URL did not return a JPEG image")
            self.resolution = (int(frame.shape[1]), int(frame.shape[0]))
            self.native_fps = float(self.options.get("snapshot_fps", 2.0))
            self._first = frame
            return
        super()._open()

    def _capture_target(self) -> str:
        return inject_credentials(self.uri, self._username, self._password)

    def _snapshot(self) -> np.ndarray | None:
        try:
            r = self.client.get(self.uri)
            if r.status_code != 200:
                self.last_error = f"snapshot HTTP {r.status_code}"
                return None
            img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
            return img
        except httpx.HTTPError as e:
            self.last_error = f"snapshot request failed: {type(e).__name__}"
            return None

    def _grab(self) -> np.ndarray | None:
        if self.options.get("mode") != "snapshot":
            return super()._grab()
        first = getattr(self, "_first", None)
        if first is not None:
            self._first = None
            return first
        time.sleep(1.0 / max(0.2, self.native_fps or 2.0))
        return self._snapshot()

    def _close(self) -> None:
        super()._close()
        client = getattr(self, "client", None)
        if client is not None:
            client.close()
            self.client = None


class WebcamSource(_CaptureSource):
    source_type = "webcam"

    def _capture_target(self) -> int | str:
        u = self.uri.strip()
        if u.isdigit():
            return int(u)
        return u  # e.g. /dev/video0

    def _backend(self) -> int:
        return cv2.CAP_ANY

    def _open_error(self) -> str:
        return f"webcam {self.uri!r} not available on the worker host"


class VideoFileSource(_CaptureSource):
    """Video file replay.

    Options: ``loop`` (default true), ``realtime`` (pace at native fps, default true) and
    ``wall_clock_sync`` – the playback position is derived from the wall clock
    (``time % duration``) so several processes replaying the same file stay in step,
    which is what the recorded demo mode uses.
    """

    source_type = "file"

    def _capture_target(self) -> str:
        return str(self._path())

    def _path(self) -> Path:
        p = Path(self.uri)
        if not p.is_absolute():
            p = get_settings().DATA_DIR / p
        return p

    def _open(self) -> None:
        p = self._path()
        if not p.exists():
            raise SourceError(f"video file not found: {p.name}", permanent=True)
        super()._open()
        assert self.cap is not None
        self.frame_count = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        self.fps_eff = self.native_fps or 25.0
        self.duration = self.frame_count / self.fps_eff if self.frame_count else 0.0
        self._next_due = time.time()
        if self.options.get("wall_clock_sync") and self.duration > 0:
            self._first = None
            self._seek_to_wall_clock()

    def _seek_to_wall_clock(self) -> None:
        assert self.cap is not None
        offset = float(self.options.get("epoch_offset_s", 0.0))
        pos = ((time.time() - offset) % self.duration) * self.fps_eff
        self.cap.set(cv2.CAP_PROP_POS_FRAMES, int(pos))
        self._pos = int(pos)

    def _grab(self) -> np.ndarray | None:
        first = getattr(self, "_first", None)
        if first is not None:
            self._first = None
            return first
        assert self.cap is not None
        if self.options.get("realtime", True):
            delay = self._next_due - time.time()
            if delay > 0:
                time.sleep(delay)
            self._next_due = max(self._next_due + 1.0 / self.fps_eff, time.time() - 0.5)
        if self.options.get("wall_clock_sync") and self.duration > 0:
            offset = float(self.options.get("epoch_offset_s", 0.0))
            want = int(((time.time() - offset) % self.duration) * self.fps_eff)
            if abs(want - getattr(self, "_pos", 0)) > self.fps_eff:  # drifted or wrapped: resync
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, want)
                self._pos = want
        ok, frame = self.cap.read()
        if not ok:
            if not self.options.get("loop", True):
                return None
            self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            self._pos = 0
            ok, frame = self.cap.read()
            if not ok:
                return None
        self._pos = getattr(self, "_pos", 0) + 1
        return frame
