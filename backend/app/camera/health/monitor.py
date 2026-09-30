"""Per-camera health state machine.

States: CONNECTING → ONLINE ⇄ DEGRADED; any → OFFLINE (no frames for ``timeout``
seconds or connect failure) or ERROR (permanent configuration error). Reconnects use
exponential backoff with jitter, capped at ``CAMERA_MAX_BACKOFF``.

DEGRADED is raised for: input FPS below ``degraded_ratio`` of the source's native
rate, processing FPS well below target (worker overloaded), sharpness collapsing
relative to the camera's own baseline (blur/defocus/rain), or a very dark image.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

ONLINE, DEGRADED, OFFLINE, CONNECTING, ERROR = "ONLINE", "DEGRADED", "OFFLINE", "CONNECTING", "ERROR"
STATUSES = (ONLINE, DEGRADED, OFFLINE, CONNECTING, ERROR)


@dataclass
class Transition:
    camera_id: str
    old: str
    new: str
    message: str
    ts: float = field(default_factory=time.time)


class HealthMonitor:
    def __init__(self, camera_id: str, *, timeout: float = 8.0, degraded_ratio: float = 0.5, max_backoff: float = 60.0,
                 grace_s: float = 6.0, degraded_hold_s: float = 4.0) -> None:
        self.camera_id = camera_id
        self.timeout = timeout
        self.degraded_ratio = degraded_ratio
        self.max_backoff = max_backoff
        self.grace_s = grace_s
        self.degraded_hold_s = degraded_hold_s
        self.status = CONNECTING
        self.message = "starting"
        self.since = time.time()
        self.connected_at: float | None = None
        self.last_frame_at: float | None = None
        self.failures = 0
        self.reconnects = 0
        self.sharpness_baseline: float | None = None
        self._degraded_candidate_since: float | None = None
        self._degraded_reason = ""
        self.transitions: list[Transition] = []

    # ------------------------------------------------------------------ transitions
    def _set(self, status: str, message: str) -> Transition | None:
        if status == self.status and message == self.message:
            return None
        old = self.status
        self.status, self.message = status, message
        if status != old:
            self.since = time.time()
            tr = Transition(self.camera_id, old, status, message)
            self.transitions.append(tr)
            return tr
        return None

    def on_connecting(self) -> Transition | None:
        return self._set(CONNECTING, "connecting" if self.failures == 0 else f"reconnecting (attempt {self.failures + 1})")

    def on_connected(self) -> Transition | None:
        if self.failures or self.connected_at is not None:
            self.reconnects += 1
        self.failures = 0
        self.connected_at = time.time()
        self.last_frame_at = time.time()
        self._degraded_candidate_since = None
        return self._set(ONLINE, "stream connected")

    def on_connect_failed(self, error: str, permanent: bool = False) -> tuple[Transition | None, float]:
        self.failures += 1
        tr = self._set(ERROR if permanent else OFFLINE, error)
        return tr, self.backoff()

    def on_stream_lost(self, error: str) -> Transition | None:
        self.connected_at = None
        return self._set(OFFLINE, error)

    def on_stopped(self) -> Transition | None:
        self.connected_at = None
        return self._set(OFFLINE, "stopped by operator")

    def backoff(self) -> float:
        base = min(self.max_backoff, 1.0 * (2 ** max(0, self.failures - 1)))
        return base * random.uniform(0.8, 1.2)

    def on_frame(self, ts: float) -> None:
        self.last_frame_at = ts

    def frame_timed_out(self, now: float | None = None) -> bool:
        now = now or time.time()
        return self.last_frame_at is not None and now - self.last_frame_at > self.timeout

    # ------------------------------------------------------------------ periodic evaluation
    def evaluate(self, *, input_fps: float, native_fps: float | None, processing_fps: float, target_processing_fps: float,
                 sharpness: float | None, brightness: float | None, now: float | None = None) -> Transition | None:
        now = now or time.time()
        if self.status not in (ONLINE, DEGRADED) or self.connected_at is None:
            return None
        if now - self.connected_at < self.grace_s:
            return None
        reasons = []
        if native_fps and input_fps < self.degraded_ratio * native_fps:
            reasons.append(f"input {input_fps:.1f} fps < {self.degraded_ratio:.0%} of native {native_fps:.1f} fps")
        if target_processing_fps > 0 and processing_fps < self.degraded_ratio * min(target_processing_fps, native_fps or target_processing_fps):
            reasons.append(f"processing {processing_fps:.1f} fps below target {target_processing_fps:.1f} fps")
        if sharpness is not None:
            if self.sharpness_baseline is None:
                self.sharpness_baseline = sharpness
            if sharpness < max(15.0, 0.25 * self.sharpness_baseline):
                reasons.append(f"image sharpness {sharpness:.0f} vs baseline {self.sharpness_baseline:.0f} (blur/defocus)")
            elif not reasons:
                self.sharpness_baseline = 0.98 * self.sharpness_baseline + 0.02 * sharpness
        if brightness is not None and brightness < 25:
            reasons.append(f"image too dark (mean luminance {brightness:.0f})")
        if reasons:
            if self._degraded_candidate_since is None:
                self._degraded_candidate_since = now
            if now - self._degraded_candidate_since >= self.degraded_hold_s or self.status == DEGRADED:
                return self._set(DEGRADED, "; ".join(reasons))
            return None
        self._degraded_candidate_since = None
        if self.status == DEGRADED:
            return self._set(ONLINE, "recovered")
        return None
