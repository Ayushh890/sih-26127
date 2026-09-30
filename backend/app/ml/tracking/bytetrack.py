"""ByteTrack multi-object tracker (Zhang et al., 2022) — compact NumPy implementation.

Two-stage association: confirmed/lost tracks are first matched to
high-confidence detections, then remaining confirmed tracks are matched to
low-confidence detections (recovering occluded/blurred vehicles instead of
spawning duplicate IDs). A constant-velocity Kalman filter over
(cx, cy, aspect, h) predicts track positions between processed frames.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from app.ml.detectors.base import Detection


class KalmanFilter:
    def __init__(self) -> None:
        ndim, dt = 4, 1.0
        self.F = np.eye(2 * ndim)
        for i in range(ndim):
            self.F[i, ndim + i] = dt
        self.H = np.eye(ndim, 2 * ndim)
        self.w_pos = 1.0 / 20
        self.w_vel = 1.0 / 160

    def initiate(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        mean = np.r_[z, np.zeros(4)]
        h = z[3]
        std = [2 * self.w_pos * h, 2 * self.w_pos * h, 1e-2, 2 * self.w_pos * h, 10 * self.w_vel * h, 10 * self.w_vel * h, 1e-5, 10 * self.w_vel * h]
        return mean, np.diag(np.square(std))

    def predict(self, mean: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        std = [self.w_pos * h, self.w_pos * h, 1e-2, self.w_pos * h, self.w_vel * h, self.w_vel * h, 1e-5, self.w_vel * h]
        Q = np.diag(np.square(std))
        return self.F @ mean, self.F @ cov @ self.F.T + Q

    def update(self, mean: np.ndarray, cov: np.ndarray, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        h = mean[3]
        R = np.diag(np.square([self.w_pos * h, self.w_pos * h, 1e-1, self.w_pos * h]))
        S = self.H @ cov @ self.H.T + R
        K = cov @ self.H.T @ np.linalg.inv(S)
        mean = mean + K @ (z - self.H @ mean)
        cov = (np.eye(len(mean)) - K @ self.H) @ cov
        return mean, cov


def xyxy_to_xyah(b: np.ndarray) -> np.ndarray:
    w, h = b[2] - b[0], b[3] - b[1]
    return np.array([b[0] + w / 2, b[1] + h / 2, w / max(h, 1e-6), h], dtype=np.float64)


def xyah_to_xyxy(m: np.ndarray) -> np.ndarray:
    w = m[2] * m[3]
    return np.array([m[0] - w / 2, m[1] - m[3] / 2, m[0] + w / 2, m[1] + m[3] / 2], dtype=np.float32)


def iou_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    if len(a) == 0 or len(b) == 0:
        return np.zeros((len(a), len(b)), dtype=np.float32)
    x1 = np.maximum(a[:, None, 0], b[None, :, 0])
    y1 = np.maximum(a[:, None, 1], b[None, :, 1])
    x2 = np.minimum(a[:, None, 2], b[None, :, 2])
    y2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(x2 - x1, 0, None) * np.clip(y2 - y1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / np.maximum(area_a[:, None] + area_b[None, :] - inter, 1e-6)


def _assign(cost: np.ndarray, thresh: float) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    if cost.size == 0:
        return [], list(range(cost.shape[0])), list(range(cost.shape[1]))
    rows, cols = linear_sum_assignment(cost)
    matches = [(r, c) for r, c in zip(rows, cols) if cost[r, c] <= thresh]
    mr = {r for r, _ in matches}
    mc = {c for _, c in matches}
    return matches, [r for r in range(cost.shape[0]) if r not in mr], [c for c in range(cost.shape[1]) if c not in mc]


TRACKED, LOST, REMOVED = 0, 1, 2


@dataclass
class STrack:
    track_id: int
    mean: np.ndarray
    cov: np.ndarray
    score: float
    det: Detection
    start_frame: int
    frame_id: int
    state: int = TRACKED
    activated: bool = False
    hits: int = 1
    label_votes: dict[str, float] = field(default_factory=lambda: defaultdict(float))

    @property
    def box(self) -> np.ndarray:
        return xyah_to_xyxy(self.mean)

    @property
    def label(self) -> str:
        return max(self.label_votes.items(), key=lambda kv: kv[1])[0] if self.label_votes else self.det.label

    @property
    def label_confidence(self) -> float:
        total = sum(self.label_votes.values())
        return (self.label_votes[self.label] / total) if total else self.det.confidence


class ByteTracker:
    def __init__(self, high_thresh: float = 0.5, low_thresh: float = 0.1, new_track_thresh: float = 0.55, match_thresh: float = 0.8, max_time_lost: int = 15) -> None:
        self.high = high_thresh
        self.low = low_thresh
        self.new_thresh = new_track_thresh
        self.match_thresh = match_thresh
        self.max_time_lost = max_time_lost
        self.kf = KalmanFilter()
        self.frame_id = 0
        self._next_id = 1
        self.tracks: list[STrack] = []
        self.removed: list[STrack] = []  # tracks removed during the last update

    def _new_id(self) -> int:
        tid = self._next_id
        self._next_id += 1
        return tid

    def _activate(self, t: STrack, d: Detection) -> None:
        t.mean, t.cov = self.kf.update(t.mean, t.cov, xyxy_to_xyah(d.xyxy))
        t.det, t.score, t.frame_id, t.state = d, d.confidence, self.frame_id, TRACKED
        t.hits += 1
        t.label_votes[d.label] += d.confidence
        if t.hits >= 2:
            t.activated = True

    def update(self, dets: list[Detection]) -> list[STrack]:
        self.frame_id += 1
        self.removed = []
        high = [d for d in dets if d.confidence >= self.high]
        low = [d for d in dets if self.low <= d.confidence < self.high]

        for t in self.tracks:
            t.mean, t.cov = self.kf.predict(t.mean, t.cov)
        confirmed = [t for t in self.tracks if t.activated or t.state == LOST]
        unconfirmed = [t for t in self.tracks if not t.activated and t.state == TRACKED]

        # stage 1: confirmed + lost vs high-confidence detections (IoU fused with score)
        pool = confirmed
        ious = iou_matrix(np.array([t.box for t in pool]).reshape(-1, 4), np.array([d.xyxy for d in high]).reshape(-1, 4))
        cost = 1 - ious * np.array([d.confidence for d in high])[None, :] if len(high) else 1 - ious
        m, u_track, u_det = _assign(cost, self.match_thresh)
        for ti, di in m:
            self._activate(pool[ti], high[di])

        # stage 2: remaining *tracked* tracks vs low-confidence detections
        remain = [pool[i] for i in u_track if pool[i].state == TRACKED]
        ious2 = iou_matrix(np.array([t.box for t in remain]).reshape(-1, 4), np.array([d.xyxy for d in low]).reshape(-1, 4))
        m2, u_track2, _ = _assign(1 - ious2, 0.5)
        for ti, di in m2:
            self._activate(remain[ti], low[di])
        for i in u_track2:
            remain[i].state = LOST
        for i in u_track:
            if pool[i].state == LOST and pool[i] not in remain:
                pass  # already lost; aged below

        # stage 3: unconfirmed tracks vs leftover high detections
        left = [high[i] for i in u_det]
        ious3 = iou_matrix(np.array([t.box for t in unconfirmed]).reshape(-1, 4), np.array([d.xyxy for d in left]).reshape(-1, 4))
        m3, u_unc, u_det3 = _assign(1 - ious3, 0.7)
        for ti, di in m3:
            self._activate(unconfirmed[ti], left[di])
        dropped = {id(unconfirmed[i]) for i in u_unc}

        # new tracks
        for i in u_det3:
            d = left[i]
            if d.confidence < self.new_thresh:
                continue
            mean, cov = self.kf.initiate(xyxy_to_xyah(d.xyxy))
            t = STrack(self._new_id(), mean, cov, d.confidence, d, self.frame_id, self.frame_id)
            t.label_votes[d.label] += d.confidence
            if self.frame_id == 1:
                t.activated = True
            self.tracks.append(t)

        survivors: list[STrack] = []
        for t in self.tracks:
            if id(t) in dropped:
                t.state = REMOVED
            elif t.state == LOST and self.frame_id - t.frame_id > self.max_time_lost:
                t.state = REMOVED
            if t.state == REMOVED:
                if t.activated:
                    self.removed.append(t)
            else:
                survivors.append(t)
        self.tracks = survivors
        return [t for t in self.tracks if t.state == TRACKED and t.activated and t.frame_id == self.frame_id]

    def flush(self) -> list[STrack]:
        """Terminate all tracks (camera stopped); returns the activated ones."""
        out = [t for t in self.tracks if t.activated]
        self.tracks = []
        return out
