"""Per-camera vision pipeline.

frame -> vehicle detection -> ByteTrack -> vehicle crop -> plate detection ->
plate rectification + enhancement -> batched OCR -> normalisation ->
temporal OCR fusion -> appearance embedding -> speed/direction/lane ->
observation event (emitted when the track leaves the scene).

The pipeline is synchronous and owned by exactly one camera worker thread.
Models are shared via the registry; every stage is individually guarded so a
failing OCR/Re-ID call degrades the observation instead of killing the camera.
"""
from __future__ import annotations

import base64
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import cv2
import numpy as np

from app.core.logging import get_logger
from app.ml.detectors.base import Detection
from app.ml.ocr.fusion import FusedPlate, PlateReadSample, fuse_reads
from app.ml.ocr.normalize import normalize_plate, weighted_edit_distance
from app.ml.pipelines.geometry import CameraGeometry, estimate_motion
from app.ml.preprocessing.image import dominant_color, enhance_plate, expand_box, frame_quality, rectify_plate
from app.ml.registry import ModelRegistry
from app.ml.tracking.bytetrack import ByteTracker, STrack

log = get_logger("ml.pipeline")


@dataclass
class PipelineConfig:
    confidence_threshold: float = 0.35
    plate_confidence: float = 0.3
    ocr_confidence_threshold: float = 0.55
    min_plate_vehicle_width: int = 90
    max_reads_per_track: int = 12
    max_plate_attempts: int = 60
    min_read_confidence: float = 0.2
    min_track_hits: int = 3
    queue_speed_kmh: float = 7.0  # vehicles slower than this count as queued (HCM uses ~5 mph)
    switch_read_confidence: float = 0.85
    switch_min_distance: float = 3.0
    switch_confirm_reads: int = 2
    min_samples_without_plate: int = 6  # plate-less tracks must be observed long enough to not be fragments
    max_track_duration_s: float = 180.0
    edge_margin: int = 6
    evidence_full_frames: bool = True
    evidence_frame_width: int = 640

    @classmethod
    def from_camera(cls, cam: dict, ocr_threshold: float, default_conf: float) -> "PipelineConfig":
        p = cam.get("processing") or {}
        return cls(
            confidence_threshold=float(p.get("confidence_threshold", default_conf)),
            plate_confidence=float(p.get("plate_confidence", 0.3)),
            ocr_confidence_threshold=float(p.get("ocr_confidence_threshold", ocr_threshold)),
            min_plate_vehicle_width=int(p.get("min_plate_vehicle_width", 90)),
            max_reads_per_track=int(p.get("max_reads_per_track", 12)),
            queue_speed_kmh=float(p.get("queue_speed_kmh", 7.0)),
            evidence_full_frames=bool(p.get("evidence_full_frames", True)),
        )


@dataclass
class TrackState:
    track_id: int
    first_ts: float
    last_ts: float
    frames: int = 0
    reads: list[PlateReadSample] = field(default_factory=list)
    plate_bboxes: list[list[float]] = field(default_factory=list)
    best_plate: tuple[float, np.ndarray, list[float]] | None = None  # (conf, crop, bbox in frame coords)
    best_crop: tuple[float, np.ndarray, list[float]] | None = None  # (quality, crop, bbox)
    motion_samples: list[tuple[float, float, float, float, str]] = field(default_factory=list)
    path: list[tuple[float, float, float]] = field(default_factory=list)
    lanes: list[int] = field(default_factory=list)
    frame_before: np.ndarray | None = None
    frame_detection: np.ndarray | None = None
    frame_after: np.ndarray | None = None
    after_countdown: int = -1
    announced: bool = False
    announced_plate: str | None = None
    emitted: bool = False
    last_plate_attempt: int = -10
    plate_attempts: int = 0
    last_box: list[float] | None = None
    last_label: str = "car"
    last_label_conf: float = 0.0
    last_speed_kmh: float | None = None
    # confident reads of a clearly different plate: evidence of a tracker identity switch
    conflicts: list[tuple[PlateReadSample, list[float], np.ndarray]] = field(default_factory=list)


@dataclass
class FrameOutput:
    events: list[dict[str, Any]]
    annotated: np.ndarray | None
    active_tracks: int
    detections: int
    occupancy: float
    stationary: int
    inference_ms: float
    stage_ms: dict[str, float]


class CameraPipeline:
    def __init__(self, camera: dict[str, Any], registry: ModelRegistry, config: PipelineConfig, evidence_writer: Any = None) -> None:
        self.camera = camera
        self.camera_id: str = camera["id"]
        self.registry = registry
        self.cfg = config
        self.detector = registry.vehicle_detector_for_camera()
        high = config.confidence_threshold
        self.tracker = ByteTracker(high_thresh=high, low_thresh=max(0.05, high * 0.4), new_track_thresh=high + 0.05, max_time_lost=10)
        self.geometry: CameraGeometry | None = None
        self.states: dict[int, TrackState] = {}
        self.ring: deque[tuple[float, np.ndarray]] = deque(maxlen=8)
        self.evidence_writer = evidence_writer
        self.frame_index = 0
        self.quality: tuple[float, float] = (0.0, 0.0)
        self.counters = {"vehicles": 0, "plates": 0, "ocr_conf_sum": 0.0, "ocr_reads": 0, "stationary_sum": 0.0, "occupancy_sum": 0.0, "frames": 0, "vehicles_in_frame_sum": 0, "max_in_frame": 0, "speed_sum": 0.0, "speed_n": 0, "track_splits": 0}

    # ------------------------------------------------------------------ public API
    def reset_counters(self) -> dict[str, Any]:
        c = self.counters
        self.counters = {k: (0.0 if isinstance(v, float) else 0) for k, v in c.items()}
        return c

    def process(self, frame: np.ndarray, ts: float, draw: bool = True) -> FrameOutput:
        t0 = time.perf_counter()
        self.frame_index += 1
        h, w = frame.shape[:2]
        if self.geometry is None or self.geometry.frame_width != w:
            self.geometry = CameraGeometry.from_camera(self.camera, w, h)
        stage: dict[str, float] = {}
        events: list[dict[str, Any]] = []

        if self.frame_index % 10 == 1:
            self.quality = frame_quality(frame)

        ts0 = time.perf_counter()
        dets = self._safe_detect(frame)
        stage["detect_ms"] = (time.perf_counter() - ts0) * 1000

        ts0 = time.perf_counter()
        tracks = self.tracker.update(dets)
        stage["track_ms"] = (time.perf_counter() - ts0) * 1000

        ts0 = time.perf_counter()
        plate_jobs: list[tuple[TrackState, np.ndarray, list[float]]] = []
        occupied = 0.0
        stationary = 0
        for t in tracks:
            st = self._update_state(t, frame, ts)
            occupied += max(0.0, (t.det.x2 - t.det.x1) * (t.det.y2 - t.det.y1))
            if st.last_speed_kmh is not None and st.last_speed_kmh < self.cfg.queue_speed_kmh and ts - st.first_ts > 2.0:
                stationary += 1
            if not st.announced and st.frames >= self.cfg.min_track_hits:
                st.announced = True
                self.counters["vehicles"] += 1
                events.append(self._vehicle_detected_event(st, ts))
            job = self._plate_candidate(t, st, frame)
            if job is not None:
                plate_jobs.append(job)
        stage["plate_detect_ms"] = (time.perf_counter() - ts0) * 1000

        ts0 = time.perf_counter()
        events.extend(self._run_ocr(plate_jobs, frame, ts))
        stage["ocr_ms"] = (time.perf_counter() - ts0) * 1000

        # finished tracks -> observations
        ts0 = time.perf_counter()
        for t in self.tracker.removed:
            ev = self._finalize(t.track_id, ts)
            if ev:
                events.append(ev)
        for st in list(self.states.values()):  # long-lived (queued) tracks are emitted early
            if not st.emitted and st.last_ts - st.first_ts > self.cfg.max_track_duration_s:
                ev = self._finalize(st.track_id, ts, keep=True)
                if ev:
                    events.append(ev)
        stage["finalize_ms"] = (time.perf_counter() - ts0) * 1000

        self.ring.append((ts, frame))
        roi_area = float(w * h * 0.6)  # lower 60% of the frame is where the road is imaged
        occupancy = min(1.0, occupied / roi_area)
        c = self.counters
        c["frames"] += 1
        c["occupancy_sum"] += occupancy
        c["stationary_sum"] += stationary
        c["vehicles_in_frame_sum"] += len(tracks)
        c["max_in_frame"] = max(c["max_in_frame"], len(tracks))

        annotated = self._draw(frame, tracks) if draw else None
        inference_ms = (time.perf_counter() - t0) * 1000
        return FrameOutput(events, annotated, len(tracks), len(dets), occupancy, stationary, inference_ms, stage)

    def flush(self, ts: float) -> list[dict[str, Any]]:
        events = []
        for t in self.tracker.flush():
            ev = self._finalize(t.track_id, ts)
            if ev:
                events.append(ev)
        self.states.clear()
        return events

    # ------------------------------------------------------------------ stages
    def _safe_detect(self, frame: np.ndarray) -> list[Detection]:
        try:
            return self.detector.detect(frame, self.tracker.low)
        except Exception:
            log.exception("vehicle detection failed", extra={"camera_id": self.camera_id})
            return []

    def _update_state(self, t: STrack, frame: np.ndarray, ts: float) -> TrackState:
        st = self.states.get(t.track_id)
        if st is None:
            st = self.states[t.track_id] = TrackState(t.track_id, ts, ts)
        st.last_ts = ts
        st.frames += 1
        d = t.det
        h, w = frame.shape[:2]
        st.last_box = [round(d.x1, 1), round(d.y1, 1), round(d.x2, 1), round(d.y2, 1)]
        st.last_label, st.last_label_conf = t.label, t.label_confidence
        cx, cy = (d.x1 + d.x2) / 2, (d.y1 + d.y2) / 2
        st.path.append((round(ts, 2), round(cx, 1), round(cy, 1)))
        m = self.cfg.edge_margin
        fully_visible = d.x1 > m and d.y1 > m and d.x2 < w - m and d.y2 < h - m
        if fully_visible:
            st.motion_samples.append((ts, cx, d.y2, d.width, t.label))
            lane = self.geometry.lane_of(cx, d.y2) if self.geometry else None
            if lane:
                st.lanes.append(lane)
            quality = d.width * d.height * d.confidence
            if st.best_crop is None or quality > st.best_crop[0]:
                x1, y1, x2, y2 = (int(v) for v in (d.x1, d.y1, d.x2, d.y2))
                st.best_crop = (quality, frame[y1:y2, x1:x2].copy(), st.last_box)
                if st.best_plate is None:  # until a plate is read, the best vehicle view is the evidence frame
                    st.frame_detection = frame
        if len(st.motion_samples) >= 3 and self.geometry is not None and st.frames % 3 == 0:
            st.last_speed_kmh = estimate_motion(self.geometry, st.motion_samples[-8:]).speed_kmh
        if st.after_countdown > 0:
            st.after_countdown -= 1
            if st.after_countdown == 0:
                st.frame_after = frame
        if len(st.path) > 400:
            st.path = st.path[::2]
        return st

    def _plate_candidate(self, t: STrack, st: TrackState, frame: np.ndarray) -> tuple[TrackState, np.ndarray, list[float]] | None:
        if self.registry.plate is None or self.registry.ocr is None:
            return None
        d = t.det
        if d.width < self.cfg.min_plate_vehicle_width or st.plate_attempts >= self.cfg.max_plate_attempts:
            return None
        # once the read buffer is full keep sampling at half rate: the vehicle is usually
        # still approaching, so later reads are larger and replace the weakest ones
        gap = 2 if len(st.reads) >= self.cfg.max_reads_per_track else 1
        if self.frame_index - st.last_plate_attempt < gap:
            return None
        st.last_plate_attempt = self.frame_index
        st.plate_attempts += 1
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = expand_box(d.x1, d.y1, d.x2, d.y2, w, h, 0.03, 0.03)
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return None
        try:
            plates = self.registry.plate.detect(crop, self.cfg.plate_confidence)
        except Exception:
            log.exception("plate detection failed", extra={"camera_id": self.camera_id})
            return None
        if not plates:
            return None
        p = plates[0]
        ch, cw = crop.shape[:2]
        px1, py1, px2, py2 = expand_box(p.x1, p.y1, p.x2, p.y2, cw, ch, 0.04, 0.12)
        pcrop = crop[py1:py2, px1:px2]
        if pcrop.size == 0 or pcrop.shape[1] < 20:
            return None
        return st, pcrop, [x1 + px1, y1 + py1, x1 + px2, y1 + py2]

    def _run_ocr(self, jobs: list[tuple[TrackState, np.ndarray, list[float]]], frame: np.ndarray, ts: float) -> list[dict[str, Any]]:
        if not jobs or self.registry.ocr is None:
            return []
        variants: list[np.ndarray] = []
        for _, pcrop, _ in jobs:
            try:
                rect = rectify_plate(pcrop)
                variants.extend([pcrop, enhance_plate(rect)])
            except Exception:
                variants.extend([pcrop, pcrop])
        try:
            results = self.registry.ocr.recognize(variants)
        except Exception:
            log.exception("OCR failed", extra={"camera_id": self.camera_id})
            return []
        events = []
        for i, (st, pcrop, pbox) in enumerate(jobs):
            a, b = results[2 * i], results[2 * i + 1]
            best = a if a.confidence >= b.confidence else b
            if not best.text or best.confidence < self.cfg.min_read_confidence:
                continue
            norm = normalize_plate(best.text)
            sample = PlateReadSample(best.text, norm.normalized, best.confidence, best.char_confidences, norm.is_valid, ts)
            verdict = self._switch_check(st, sample)
            if verdict == "hold":
                st.conflicts.append((sample, pbox, pcrop.copy()))
                continue
            if verdict == "split":
                st.conflicts.append((sample, pbox, pcrop.copy()))
                ev, new = self._split_track(st, ts, frame)
                if ev:
                    events.append(ev)
                fused = fuse_reads(new.reads)
                if fused and fused.confidence >= self.cfg.ocr_confidence_threshold:
                    new.announced_plate = fused.text
                    events.append(self._plate_read_event(new, fused, new.reads[-1], ts))
                continue
            st.conflicts.clear()
            if len(st.reads) < self.cfg.max_reads_per_track:
                st.reads.append(sample)
                st.plate_bboxes.append(pbox)
            else:
                worst = min(range(len(st.reads)), key=lambda k: st.reads[k].confidence)
                if st.reads[worst].confidence >= best.confidence:
                    continue
                st.reads[worst], st.plate_bboxes[worst] = sample, pbox
            self.counters["ocr_reads"] += 1
            self.counters["ocr_conf_sum"] += best.confidence
            if st.best_plate is None or best.confidence > st.best_plate[0]:
                st.best_plate = (best.confidence, pcrop.copy(), pbox)
                st.frame_detection = frame
                st.frame_before = self._frame_before(ts)
                st.frame_after = None
                st.after_countdown = 3
            fused = fuse_reads(st.reads)
            if fused and fused.confidence >= self.cfg.ocr_confidence_threshold and fused.text != st.announced_plate:
                st.announced_plate = fused.text
                events.append(self._plate_read_event(st, fused, sample, ts))
        return events

    def _switch_check(self, st: TrackState, sample: PlateReadSample) -> str:
        """``ok`` | ``hold`` | ``split``. At low processing rates ByteTrack can hand a track
        over to the next vehicle in the same lane. A track that already has a corroborated
        plate and then gets consecutive confident, valid reads of a clearly different
        plate is split instead of letting the two plates dilute each other's vote."""
        if not sample.is_valid or sample.confidence < self.cfg.switch_read_confidence:
            return "ok"
        fused = fuse_reads(st.reads)
        if fused is None or fused.votes < 3 or fused.confidence < self.cfg.ocr_confidence_threshold:
            return "ok"
        if weighted_edit_distance(sample.normalized, fused.text) < self.cfg.switch_min_distance:
            return "ok"
        if st.conflicts and weighted_edit_distance(sample.normalized, st.conflicts[-1][0].normalized) > 1.0:
            st.conflicts.clear()  # the conflicting reads don't agree with each other either
        return "split" if len(st.conflicts) + 1 >= self.cfg.switch_confirm_reads else "hold"

    def _split_track(self, st: TrackState, ts: float, frame: np.ndarray) -> tuple[dict[str, Any] | None, TrackState]:
        """Close the current state as its own observation and start a new one for the same
        tracker id, seeded with the conflicting reads and the motion recorded since."""
        cut = st.conflicts[0][0].ts
        new = TrackState(st.track_id, cut, ts)
        new.path = [p for p in st.path if p[0] >= cut]
        new.motion_samples = [m for m in st.motion_samples if m[0] >= cut]
        new.lanes = st.lanes[-len(new.motion_samples):] if new.motion_samples else []
        new.frames = max(1, len(new.path))
        new.last_box, new.last_label, new.last_label_conf = st.last_box, st.last_label, st.last_label_conf
        new.last_plate_attempt, new.plate_attempts = st.last_plate_attempt, len(st.conflicts)
        new.announced = new.frames >= self.cfg.min_track_hits
        if new.announced:
            self.counters["vehicles"] += 1
        if st.last_box is not None:
            x1, y1, x2, y2 = (int(v) for v in st.last_box)
            crop = frame[max(0, y1):y2, max(0, x1):x2]
            if crop.size:
                new.best_crop = (float(crop.shape[0] * crop.shape[1]), crop.copy(), st.last_box)
        for sample, pbox, pcrop in st.conflicts:
            new.reads.append(sample)
            new.plate_bboxes.append(pbox)
            if new.best_plate is None or sample.confidence > new.best_plate[0]:
                new.best_plate = (sample.confidence, pcrop, pbox)
        new.frame_detection, new.frame_before, new.after_countdown = frame, self._frame_before(ts), 3
        st.conflicts = []
        st.path = [p for p in st.path if p[0] < cut] or st.path[:1]
        st.motion_samples = [m for m in st.motion_samples if m[0] < cut]
        st.last_ts = min(st.last_ts, cut)
        self.counters["track_splits"] += 1
        log.info("track identity switch: split on plate change", extra={"camera_id": self.camera_id, "track_id": st.track_id})
        ev = self._finalize(st.track_id, ts)  # pops the old state
        self.states[st.track_id] = new
        return ev, new

    def _frame_before(self, ts: float) -> np.ndarray | None:
        for rts, fr in reversed(self.ring):
            if ts - rts >= 0.8:
                return fr
        return self.ring[0][1] if self.ring else None

    # ------------------------------------------------------------------ events
    def _base(self, st: TrackState) -> dict[str, Any]:
        return {"camera_id": self.camera_id, "local_track_id": st.track_id, "is_demo": bool(self.camera.get("is_demo"))}

    def _vehicle_detected_event(self, st: TrackState, ts: float) -> dict[str, Any]:
        return {"type": "vehicle_detected", **self._base(st), "ts": ts, "vehicle_class": st.last_label, "confidence": round(st.last_label_conf, 3), "bbox": st.last_box}

    def _plate_read_event(self, st: TrackState, fused: FusedPlate, sample: PlateReadSample, ts: float) -> dict[str, Any]:
        self.counters["plates"] += 1
        return {
            "type": "plate_read", **self._base(st), "ts": ts,
            "plate_text": fused.text, "confidence": fused.confidence, "votes": fused.votes,
            "raw_text": sample.raw, "is_valid": fused.is_valid,
        }

    def _finalize(self, track_id: int, ts: float, keep: bool = False) -> dict[str, Any] | None:
        st = self.states.get(track_id) if keep else self.states.pop(track_id, None)
        if st is None or st.emitted:
            return None
        st.emitted = True
        if st.frames < self.cfg.min_track_hits or st.best_crop is None:
            return None
        fused = fuse_reads(st.reads)
        if fused is None and len(st.motion_samples) < self.cfg.min_samples_without_plate:
            return None
        geo = self.geometry
        label = st.last_label
        samples = [(t, cx, by, bw, label) for (t, cx, by, bw, _l) in st.motion_samples]  # use the track's voted class
        motion = estimate_motion(geo, samples) if geo else None
        if motion is not None and motion.speed_kmh is not None:
            self.counters["speed_sum"] += motion.speed_kmh
            self.counters["speed_n"] += 1
        crop = st.best_crop[1]
        color, rgb = ("unknown", [0, 0, 0])
        try:
            color, rgb = dominant_color(crop)
        except Exception:
            log.exception("colour estimation failed", extra={"camera_id": self.camera_id})
        embedding_b64 = None
        if self.registry.reid is not None and crop.size:
            try:
                emb = self.registry.reid.embed([crop])[0]
                embedding_b64 = base64.b64encode(emb.astype(np.float32).tobytes()).decode()
            except Exception:
                log.exception("re-id embedding failed", extra={"camera_id": self.camera_id})
        lane = max(set(st.lanes), key=st.lanes.count) if st.lanes else None

        reads = [
            {"raw_text": r.raw, "normalized_text": r.normalized, "confidence": round(r.confidence, 4), "char_confidences": [round(c, 4) for c in r.char_confidences],
             "is_valid": r.is_valid, "ts": r.ts, "plate_bbox": st.plate_bboxes[i] if i < len(st.plate_bboxes) else None,
             "corrections": [c.as_dict() for c in normalize_plate(r.raw).corrections]}
            for i, r in enumerate(st.reads)
        ]
        plate_ok = fused is not None and fused.confidence >= self.cfg.ocr_confidence_threshold * 0.6
        event: dict[str, Any] = {
            "type": "observation",
            "event_id": str(uuid.uuid4()),
            **self._base(st),
            "first_seen": st.first_ts,
            "last_seen": st.last_ts,
            "observed_at": max(st.reads, key=lambda r: r.confidence).ts if st.reads else (st.first_ts + st.last_ts) / 2,
            "frames": st.frames,
            "vehicle_class": st.last_label,
            "class_confidence": round(st.last_label_conf, 3),
            "vehicle_color": color,
            "color_rgb": rgb,
            "bbox": st.best_crop[2],
            "path": st.path[:: max(1, len(st.path) // 40)],
            "speed_kmh": motion.speed_kmh if motion else None,
            "motion": motion.motion if motion else "unknown",
            "direction": motion.direction if motion else None,
            "heading_deg": motion.heading_deg if motion else None,
            "lane": lane,
            "plate_text": fused.text if plate_ok and fused else None,
            "plate_raw": fused.raw_best if plate_ok and fused else None,
            "plate_confidence": fused.confidence if fused else None,
            "plate_valid": bool(fused.is_valid) if fused else False,
            "plate_votes": fused.votes if fused else 0,
            "plate_reads": reads,
            "embedding": embedding_b64,
            "embedding_model": self.registry.reid_model_name,
            "evidence": None,
        }
        if self.evidence_writer is not None:
            try:
                event["evidence"] = self.evidence_writer.write(self.camera_id, event, st, self.cfg)
            except Exception:
                log.exception("evidence write failed", extra={"camera_id": self.camera_id})
        return event

    # ------------------------------------------------------------------ overlay
    def _draw(self, frame: np.ndarray, tracks: list[STrack]) -> np.ndarray:
        out = frame.copy()
        for t in tracks:
            st = self.states.get(t.track_id)
            d = t.det
            color = (60, 200, 255) if st and st.announced_plate else (80, 220, 80)
            cv2.rectangle(out, (int(d.x1), int(d.y1)), (int(d.x2), int(d.y2)), color, 2)
            label = f"#{t.track_id} {t.label} {d.confidence:.2f}"
            if st and st.last_speed_kmh is not None:
                label += f" {st.last_speed_kmh:.0f}km/h"
            (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            cv2.rectangle(out, (int(d.x1), int(d.y1) - th - 6), (int(d.x1) + tw + 4, int(d.y1)), color, -1)
            cv2.putText(out, label, (int(d.x1) + 2, int(d.y1) - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA)
            if st and st.plate_bboxes:
                px1, py1, px2, py2 = (int(v) for v in st.plate_bboxes[-1])
                cv2.rectangle(out, (px1, py1), (px2, py2), (0, 170, 255), 2)
                fused = fuse_reads(st.reads)
                if fused:
                    txt = f"{fused.text} {fused.confidence:.0%}"
                    (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
                    cv2.rectangle(out, (px1, py2 + 2), (px1 + tw + 4, py2 + th + 8), (0, 0, 0), -1)
                    cv2.putText(out, txt, (px1 + 2, py2 + th + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
        return out
