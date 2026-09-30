"""One thread per camera: source → pipeline → bus.

A worker owns its source, health monitor and pipeline. Every exception inside the
loop is caught and turned into a health transition, so a failing camera can never
affect other cameras or the process.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

import cv2
import numpy as np

from app.camera.health import DEGRADED, ONLINE, HealthMonitor, Transition
from app.camera.sources import CameraSource, SourceError, create_source
from app.core.bus import EventBus, ui_event
from app.core.config import get_settings
from app.core.logging import get_logger
from app.ml.pipelines.anpr_pipeline import CameraPipeline, PipelineConfig
from app.ml.preprocessing.image import frame_quality
from app.ml.registry import ModelRegistry

log = get_logger("worker.stream")


def parse_resolution(res: str | None) -> tuple[int, int] | None:
    if not res or "x" not in res.lower():
        return None
    try:
        w, h = (int(v) for v in res.lower().split("x", 1))
        return (w, h) if w > 0 and h > 0 else None
    except ValueError:
        return None


class StreamWorker:
    def __init__(self, camera: dict[str, Any], registry: ModelRegistry, bus: EventBus, *, evidence_writer: Any = None,
                 spool: Any = None, ocr_threshold: float | None = None, bucket_seconds: int = 60) -> None:
        s = get_settings()
        self.camera = camera
        self.camera_id: str = camera["id"]
        self.bus = bus
        self.spool = spool
        self.registry = registry
        self.evidence_writer = evidence_writer
        proc = camera.get("processing") or {}
        self.processing_fps = float(proc.get("processing_fps", s.DEFAULT_PROCESSING_FPS))
        self.frame_skip = int(proc.get("frame_skip", 0))
        self.max_queue_size = int(proc.get("max_queue_size", s.DEFAULT_MAX_QUEUE_SIZE))
        self.target_res = parse_resolution(camera.get("resolution"))
        self.ocr_threshold = ocr_threshold if ocr_threshold is not None else s.OCR_CONFIDENCE_THRESHOLD
        self.bucket_seconds = bucket_seconds
        self.live_interval = 1.0 / max(0.5, s.LIVE_FRAME_FPS)
        self.live_width = s.LIVE_FRAME_WIDTH
        self.health = HealthMonitor(self.camera_id, timeout=s.CAMERA_TIMEOUT, degraded_ratio=s.CAMERA_DEGRADED_FPS_RATIO, max_backoff=s.CAMERA_MAX_BACKOFF)
        self.source: CameraSource = create_source(
            self.camera_id, camera["source_type"], camera["source_uri"], username=camera.get("username"),
            password=camera.get("password"), max_queue_size=self.max_queue_size, options=proc.get("source_options") or {},
        )
        self.pipeline = self._new_pipeline()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # metrics
        self.frames_processed = 0
        self.frames_skipped = 0
        self.processed_at: list[float] = []
        self.latency_ms = 0.0
        self.inference_ms = 0.0
        self.stage_ms: dict[str, float] = {}
        self.sharpness: float | None = None
        self.brightness: float | None = None
        self.last_error: str | None = None
        self.started_at = time.time()
        self._bucket_start = self._bucket_floor(time.time())
        self._last_live = 0.0
        self._last_runtime = 0.0
        self._last_health_sample = time.time()
        self._health_counters = {"vehicles": 0, "plates": 0, "ocr_conf_sum": 0.0, "ocr_reads": 0}
        self._last_eval = 0.0
        self._last_dropped = 0

    def _new_pipeline(self) -> CameraPipeline:
        s = get_settings()
        cfg = PipelineConfig.from_camera(self.camera, self.ocr_threshold, s.DEFAULT_CONFIDENCE_THRESHOLD)
        return CameraPipeline(self.camera, self.registry, cfg, evidence_writer=self.evidence_writer)

    def _bucket_floor(self, ts: float) -> float:
        return ts - (ts % self.bucket_seconds)

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"worker-{self.camera_id}", daemon=True)
        self._thread.start()

    def stop(self, reason: str = "stopped by operator") -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5.0)
        try:
            self.source.stop()
        except Exception:
            log.exception("source stop failed", extra={"camera_id": self.camera_id})
        for ev in self.pipeline.flush(time.time()):
            self._handle_event(ev)
        self._emit_traffic_sample(time.time(), force=True)
        tr = self.health.on_stopped()
        if tr and reason:
            tr.message = reason
            self.health.message = reason
        self._publish_transition(tr, intentional=True)
        self.bus.remove_runtime(self.camera_id)

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def inject_fault(self, kind: str, seconds: float) -> None:
        self.source.inject_fault(kind, seconds)
        if kind == "offline":  # force the disconnect now rather than waiting for the grabber
            self.source._alive = False

    # ------------------------------------------------------------------ main loop
    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.source.is_alive():
                    if not self._connect():
                        continue
                got = self.source.read_frame(timeout=0.5)
                now = time.time()
                if got is None:
                    if self.health.frame_timed_out(now) or not self.source.is_alive():
                        self._lost(self.source.last_error or f"no frames for {self.health.timeout:.0f}s")
                    self._periodic(now)
                    continue
                frame, ts = got
                self.health.on_frame(ts)
                if self._should_process(ts):
                    self._process(frame, ts)
                else:
                    self.frames_skipped += 1
                self._periodic(time.time())
            except Exception as e:  # noqa: BLE001 - isolate every failure to this camera
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("stream worker error", extra={"camera_id": self.camera_id})
                self._lost(f"worker error: {type(e).__name__}")
                self._stop.wait(1.0)

    def _connect(self) -> bool:
        self._publish_transition(self.health.on_connecting())
        try:
            self.source.connect()
        except SourceError as e:
            tr, delay = self.health.on_connect_failed(str(e), e.permanent)
            self._publish_transition(tr)
            self._put_runtime(time.time(), force=True)
            log.warning("connect failed; retry in %.1fs", delay, extra={"camera_id": self.camera_id, "status": str(e)})
            self._stop.wait(delay if not e.permanent else max(delay, 15.0))
            return False
        except Exception as e:  # noqa: BLE001
            tr, delay = self.health.on_connect_failed(f"{type(e).__name__}: {e}", False)
            self._publish_transition(tr)
            self._stop.wait(delay)
            return False
        self._publish_transition(self.health.on_connected())
        log.info("camera connected", extra={"camera_id": self.camera_id, "status": self.source.describe().get("resolution")})
        return True

    def _lost(self, message: str) -> None:
        for ev in self.pipeline.flush(time.time()):
            self._handle_event(ev)
        try:
            self.source.stop()
        except Exception:
            pass
        self._publish_transition(self.health.on_stream_lost(message))
        self._put_runtime(time.time(), force=True)

    def _should_process(self, ts: float) -> bool:
        if self.frame_skip > 0 and (self.source.stats.frames_in % (self.frame_skip + 1)) != 0:
            return False
        if self.processed_at and ts - self.processed_at[-1] < 0.95 / max(0.1, self.processing_fps):
            return False
        return True

    def _process(self, frame: np.ndarray, ts: float) -> None:
        if self.target_res and (frame.shape[1], frame.shape[0]) != self.target_res:
            frame = cv2.resize(frame, self.target_res, interpolation=cv2.INTER_AREA)
        publish_live = ts - self._last_live >= self.live_interval
        out = self.pipeline.process(frame, ts, draw=publish_live)
        self.frames_processed += 1
        self.processed_at.append(ts)
        if len(self.processed_at) > 60:
            del self.processed_at[:-60]
        self.inference_ms = 0.8 * self.inference_ms + 0.2 * out.inference_ms if self.inference_ms else out.inference_ms
        self.latency_ms = (time.time() - ts) * 1000
        self.stage_ms = out.stage_ms
        q = self.pipeline.quality
        self.sharpness, self.brightness = q if q != (0.0, 0.0) else (None, None)
        for ev in out.events:
            self._handle_event(ev)
        if publish_live:
            self._last_live = ts
            self._publish_frames(frame, out.annotated)

    # ------------------------------------------------------------------ outputs
    def _handle_event(self, ev: dict[str, Any]) -> None:
        typ = ev.get("type")
        if typ == "observation":
            self._ingest(ev)
        elif typ in ("vehicle_detected", "plate_read"):
            data = {k: v for k, v in ev.items() if k != "type"}
            if typ == "plate_read":  # raw plate text is only delivered to authorised clients (ws layer masks it)
                data["sensitive"] = True
            self.bus.publish_ui(ui_event(typ, data))

    def _ingest(self, ev: dict[str, Any]) -> None:
        ok = self.bus.publish_ingest(ev)
        if not ok and self.spool is not None:
            self.spool.put(ev)

    def _publish_frames(self, frame: np.ndarray, annotated: np.ndarray | None) -> None:
        def enc(img: np.ndarray) -> bytes | None:
            h, w = img.shape[:2]
            if w > self.live_width:
                img = cv2.resize(img, (self.live_width, int(h * self.live_width / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 70])
            return buf.tobytes() if ok else None

        raw = enc(frame)
        if raw:
            self.bus.put_frame(self.camera_id, "raw", raw)
        if annotated is not None:
            ann = enc(annotated)
            if ann:
                self.bus.put_frame(self.camera_id, "annotated", ann)

    def _publish_transition(self, tr: Transition | None, intentional: bool = False) -> None:
        if tr is None:
            return
        log.info("camera status %s -> %s", tr.old, tr.new, extra={"camera_id": self.camera_id, "status": tr.message})
        # intentional = stopped by an operator or a clean shutdown: recorded, but never an OFFLINE alert
        payload = {"camera_id": self.camera_id, "old_status": tr.old, "status": tr.new, "message": tr.message,
                   "ts": tr.ts, "is_demo": bool(self.camera.get("is_demo")), "intentional": intentional}
        self.bus.publish_ui(ui_event("camera_status_changed", payload))
        self._ingest({"type": "camera_status", **payload})

    def processing_fps_measured(self) -> float:
        now = time.time()
        recent = [t for t in self.processed_at if now - t <= 5.0]
        if len(recent) < 2:
            return 0.0
        return len(recent) / max(now - recent[0], 1e-3)

    def _periodic(self, now: float) -> None:
        if now - self._last_eval >= 1.0:
            self._last_eval = now
            d = self.source.describe()
            tr = self.health.evaluate(
                input_fps=d["input_fps"], native_fps=self.source.native_fps, processing_fps=self.processing_fps_measured(),
                target_processing_fps=self.processing_fps, sharpness=self.sharpness, brightness=self.brightness, now=now,
            )
            self._publish_transition(tr)
        self._put_runtime(now)
        if now - self._bucket_start >= self.bucket_seconds:
            self._emit_traffic_sample(now)
        if now - self._last_health_sample >= 15.0:
            self._emit_health_sample(now)

    def runtime_state(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        d = self.source.describe()
        return {
            "camera_id": self.camera_id,
            "status": self.health.status,
            "message": self.health.message,
            "status_since": self.health.since,
            "worker_id": get_settings().WORKER_ID,
            "input_fps": d["input_fps"],
            "native_fps": d["native_fps"],
            "processing_fps": round(self.processing_fps_measured(), 2),
            "target_processing_fps": self.processing_fps,
            "latency_ms": round(self.latency_ms, 1),
            "inference_ms": round(self.inference_ms, 1),
            "stage_ms": {k: round(v, 1) for k, v in self.stage_ms.items()},
            "queue_depth": d["queue_depth"],
            "max_queue_size": self.max_queue_size,
            "frames_in": d["frames_in"],
            "frames_processed": self.frames_processed,
            "frames_dropped": d["frames_dropped"] + self.frames_skipped,
            "resolution": d["resolution"],
            "reconnects": self.health.reconnects,
            "sharpness": round(self.sharpness, 1) if self.sharpness is not None else None,
            "sharpness_baseline": round(self.health.sharpness_baseline, 1) if self.health.sharpness_baseline else None,
            "brightness": round(self.brightness, 1) if self.brightness is not None else None,
            "active_tracks": len(self.pipeline.states),
            "faults": d["faults"],
            "last_error": self.last_error or d["last_error"],
            "uptime_s": round(now - self.started_at, 1),
            "updated_at": now,
        }

    def _put_runtime(self, now: float, force: bool = False) -> None:
        if not force and now - self._last_runtime < 1.0:
            return
        self._last_runtime = now
        self.bus.put_runtime(self.camera_id, self.runtime_state(now))

    def _emit_traffic_sample(self, now: float, force: bool = False) -> None:
        c = self.pipeline.reset_counters()
        for k in self._health_counters:
            self._health_counters[k] += c.get(k, 0)
        frames = c["frames"]
        start = self._bucket_start
        self._bucket_start = self._bucket_floor(now)
        if frames == 0 and not c["vehicles"]:
            return
        sample = {
            "type": "traffic_sample",
            "camera_id": self.camera_id,
            "is_demo": bool(self.camera.get("is_demo")),
            "bucket_start": datetime.fromtimestamp(start, tz=timezone.utc).isoformat(),
            "bucket_seconds": int(round(now - start)) if force else self.bucket_seconds,
            "frames": frames,
            "new_tracks": c["vehicles"],
            "plates": c["plates"],
            "avg_vehicles_in_frame": round(c["vehicles_in_frame_sum"] / frames, 3) if frames else 0.0,
            "max_vehicles_in_frame": c["max_in_frame"],
            "occupancy": round(c["occupancy_sum"] / frames, 4) if frames else 0.0,
            "avg_stationary": round(c["stationary_sum"] / frames, 3) if frames else 0.0,
            "avg_speed_kmh": round(c["speed_sum"] / c["speed_n"], 2) if c["speed_n"] else None,
            "speed_samples": c["speed_n"],
        }
        self._ingest(sample)

    def _cumulative(self) -> dict[str, float]:
        pc = self.pipeline.counters
        return {k: self._health_counters[k] + pc.get(k, 0) for k in self._health_counters}

    def _emit_health_sample(self, now: float) -> None:
        self._last_health_sample = now
        cum = self._cumulative()
        prev = getattr(self, "_health_prev", {k: 0 for k in cum})
        hc = {k: cum[k] - prev.get(k, 0) for k in cum}  # delta since the previous health sample
        self._health_prev = cum
        rt = self.runtime_state(now)
        self._ingest({
            "type": "health_sample",
            "camera_id": self.camera_id,
            "ts": now,
            "status": rt["status"],
            "input_fps": rt["input_fps"],
            "processing_fps": rt["processing_fps"],
            "latency_ms": rt["latency_ms"],
            "inference_ms": rt["inference_ms"],
            "queue_depth": rt["queue_depth"],
            "frames_processed": rt["frames_processed"],
            "frames_dropped": rt["frames_dropped"],
            "vehicles_detected": int(hc["vehicles"]),
            "plates_read": int(hc["plates"]),
            "avg_ocr_confidence": round(hc["ocr_conf_sum"] / hc["ocr_reads"], 4) if hc["ocr_reads"] else None,
            "ocr_reads": int(hc["ocr_reads"]),
            "blur_score": rt["sharpness"],
            "brightness": rt["brightness"],
            "reconnects": rt["reconnects"],
            "error": rt["last_error"],
        })
