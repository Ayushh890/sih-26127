"""Camera source interface.

Every input (RTSP, HTTP/MJPEG, webcam, browser upload, video file, synthetic demo) implements the same
contract so the stream worker, health monitor and pipeline are identical for all of them:

``connect()`` · ``read_frame(timeout)`` · ``is_alive()`` · ``stop()`` · ``reconnect()``

Sources run a background *grabber* thread that pulls frames as fast as the device
delivers them into a bounded queue (``max_queue_size``). When the processor falls
behind, the oldest frames are dropped (and counted) instead of accumulating latency.
"""
from __future__ import annotations

import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from app.core.logging import get_logger

log = get_logger("camera.source")

FAULT_KINDS = ("offline", "blur", "low_fps", "dark")


class SourceError(RuntimeError):
    """Raised by ``connect`` when a source cannot be opened.

    ``permanent`` marks configuration errors (missing file, bad URI) that retrying
    will not fix quickly; the health monitor reports those as ERROR instead of OFFLINE.
    """

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


@dataclass
class Fault:
    kind: str
    until: float


@dataclass
class SourceStats:
    frames_in: int = 0
    frames_dropped: int = 0
    connects: int = 0
    last_frame_ts: float = 0.0
    arrivals: deque = field(default_factory=lambda: deque(maxlen=120))

    def fps(self, window: float = 5.0) -> float:
        now = time.time()
        recent = [t for t in self.arrivals if now - t <= window]
        if len(recent) < 2:
            return 0.0
        span = max(now - recent[0], 1e-3)
        return len(recent) / span


class CameraSource(ABC):
    source_type = "abstract"
    # upper bound on the reconnect backoff (None = the health monitor's CAMERA_MAX_BACKOFF)
    max_retry_delay: float | None = None

    def __init__(self, camera_id: str, uri: str, *, username: str | None = None, password: str | None = None,
                 max_queue_size: int = 4, options: dict[str, Any] | None = None) -> None:
        self.camera_id = camera_id
        self.uri = uri
        self._username = username
        self._password = password
        self.options = options or {}
        self._queue: deque[tuple[np.ndarray, float]] = deque(maxlen=max(1, max_queue_size))
        self._cond = threading.Condition()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._alive = False
        self.last_error: str | None = None
        self.stats = SourceStats()
        self.native_fps: float | None = None
        self.resolution: tuple[int, int] | None = None
        self._faults: dict[str, Fault] = {}

    # ------------------------------------------------------------------ contract
    def connect(self) -> None:
        """Open the device and start the grabber thread. Raises :class:`SourceError`."""
        self.stop()
        self._stop.clear()
        if self._fault_active("offline"):
            raise SourceError("simulated disconnect (fault injection)")
        self._open()
        self.stats.connects += 1
        self._alive = True
        self.last_error = None
        self._thread = threading.Thread(target=self._grab_loop, name=f"grab-{self.camera_id}", daemon=True)
        self._thread.start()

    def read_frame(self, timeout: float = 1.0) -> tuple[np.ndarray, float] | None:
        """Oldest queued frame and its capture timestamp, or ``None`` on timeout."""
        with self._cond:
            if not self._queue:
                self._cond.wait(timeout)
            if not self._queue:
                return None
            frame, ts = self._queue.popleft()
        return self._apply_faults(frame), ts

    def is_alive(self) -> bool:
        return self._alive and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=3.0)
        self._thread = None
        self._alive = False
        try:
            self._close()
        except Exception:  # pragma: no cover - device specific
            log.debug("close failed", exc_info=True, extra={"camera_id": self.camera_id})
        with self._cond:
            self._queue.clear()

    def reconnect(self) -> None:
        self.stop()
        self.connect()

    # ------------------------------------------------------------------ helpers for subclasses
    @abstractmethod
    def _open(self) -> None: ...

    @abstractmethod
    def _grab(self) -> np.ndarray | None:
        """Blocking read of the next frame; ``None`` means the stream ended/failed."""

    def _close(self) -> None:
        return None

    def _grab_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                frame = self._grab()
            except Exception as e:  # device errors must not escape the thread
                frame = None
                self.last_error = str(e)
            if self._stop.is_set():
                break
            if self._fault_active("offline"):
                self._alive = False
                self.last_error = "simulated disconnect (fault injection)"
                break
            if frame is None:
                failures += 1
                if failures >= int(self.options.get("max_read_failures", 25)):
                    self._alive = False
                    self.last_error = self.last_error or "stream stopped delivering frames"
                    break
                time.sleep(0.02)
                continue
            failures = 0
            if self._fault_active("low_fps") and time.time() - self.stats.last_frame_ts < 1.0:
                continue  # simulated congestion on the link: ~1 fps get through
            now = time.time()
            self.stats.frames_in += 1
            self.stats.last_frame_ts = now
            self.stats.arrivals.append(now)
            self.resolution = (int(frame.shape[1]), int(frame.shape[0]))
            with self._cond:
                if len(self._queue) == self._queue.maxlen:
                    self.stats.frames_dropped += 1
                self._queue.append((frame, now))
                self._cond.notify()
        self._alive = False

    # ------------------------------------------------------------------ fault injection
    def inject_fault(self, kind: str, seconds: float) -> None:
        if kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind {kind!r}; expected one of {FAULT_KINDS}")
        self._faults[kind] = Fault(kind, time.time() + max(1.0, float(seconds)))
        log.warning("fault injected", extra={"camera_id": self.camera_id, "status": f"{kind} for {seconds:.0f}s"})

    def clear_faults(self) -> None:
        self._faults.clear()

    def active_faults(self) -> dict[str, float]:
        now = time.time()
        return {k: round(f.until - now, 1) for k, f in self._faults.items() if f.until > now}

    def _fault_active(self, kind: str) -> bool:
        f = self._faults.get(kind)
        if f is None:
            return False
        if f.until <= time.time():
            self._faults.pop(kind, None)
            return False
        return True

    def _apply_faults(self, frame: np.ndarray) -> np.ndarray:
        if self._fault_active("blur"):
            k = max(15, (frame.shape[1] // 60) | 1)
            frame = cv2.GaussianBlur(frame, (k, k), 0)
        if self._fault_active("dark"):
            frame = (frame * 0.15).astype(np.uint8)
        return frame

    # ------------------------------------------------------------------ description (never includes credentials)
    def describe(self) -> dict[str, Any]:
        return {
            "source_type": self.source_type,
            "uri": safe_uri(self.uri),
            "alive": self.is_alive(),
            "native_fps": self.native_fps,
            "input_fps": round(self.stats.fps(), 2),
            "resolution": f"{self.resolution[0]}x{self.resolution[1]}" if self.resolution else None,
            "frames_in": self.stats.frames_in,
            "frames_dropped": self.stats.frames_dropped,
            "queue_depth": len(self._queue),
            "connects": self.stats.connects,
            "last_error": self.last_error,
            "faults": self.active_faults(),
        }


def safe_uri(uri: str) -> str:
    """Strip embedded credentials from a URI for display/logging."""
    from app.core.logging import scrub

    return scrub(uri or "")
