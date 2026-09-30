"""Stream manager: keeps one :class:`StreamWorker` per enabled camera.

* reconciles the running workers with the ``cameras`` table every few seconds
  (enable/disable/config edits made through the API take effect automatically);
* executes control commands from the API (start/stop/restart/fault injection);
* writes a worker heartbeat and replays the dead-letter spool when the bus recovers.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any

from app.core.bus import EventBus
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_str
from app.db.models import Camera, WorkerHeartbeat
from app.db.session import session_scope
from app.evidence.store import get_evidence_store
from app.ml.registry import ModelRegistry, get_registry
from app.services.settings_service import settings_cache
from app.workers.spool import Spool
from app.workers.stream_worker import StreamWorker

log = get_logger("worker.manager")

_CONFIG_FIELDS = ("source_type", "source_uri", "username", "password_encrypted", "resolution", "lane_count", "direction",
                  "processing", "calibration")


def camera_worker_config(cam: Camera) -> dict[str, Any]:
    return {
        "id": cam.id, "name": cam.name, "source_type": cam.source_type, "source_uri": cam.source_uri,
        "username": cam.username, "password": decrypt_str(cam.password_encrypted), "resolution": cam.resolution,
        "lane_count": cam.lane_count, "direction": cam.direction, "processing": dict(cam.processing or {}),
        "calibration": dict(cam.calibration or {}), "is_demo": cam.is_demo,
    }


def config_signature(cam: Camera) -> str:
    blob = json.dumps({f: getattr(cam, f) for f in _CONFIG_FIELDS}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def parse_shard(spec: str) -> tuple[int, int]:
    index, count = (int(x) for x in spec.split("/"))
    if count < 1 or not 0 <= index < count:
        raise ValueError(f"WORKER_SHARD {spec!r}: expected index/count with 0 <= index < count")
    return index, count


def owns_camera(camera_id: str, shard: tuple[int, int]) -> bool:
    """Stable camera → worker assignment (same result in every process)."""
    index, count = shard
    return count == 1 or int(hashlib.sha1(camera_id.encode()).hexdigest(), 16) % count == index


class StreamManager:
    def __init__(self, bus: EventBus, registry: ModelRegistry | None = None, reconcile_interval: float = 5.0) -> None:
        self.bus = bus
        self.registry = registry
        self.reconcile_interval = reconcile_interval
        self.workers: dict[str, StreamWorker] = {}
        self.signatures: dict[str, str] = {}
        self.spool = Spool()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.worker_id = get_settings().WORKER_ID
        self.shard = parse_shard(get_settings().WORKER_SHARD)
        self.started_at = time.time()

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        import os

        # errors only: FFmpeg/OpenCV reconnect noise is reported through health states instead
        os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")
        os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "16")
        try:
            import cv2

            set_level = getattr(cv2, "setLogLevel", None) or getattr(getattr(cv2, "utils", None), "logging", None) and cv2.utils.logging.setLogLevel
            if set_level:
                set_level(2)
        except Exception:
            pass
        if self.registry is None:
            self.registry = get_registry()
        self._stop.clear()
        for name, fn in (("reconcile", self._reconcile_loop), ("control", self._control_loop), ("heartbeat", self._heartbeat_loop)):
            t = threading.Thread(target=fn, name=f"manager-{name}", daemon=True)
            t.start()
            self._threads.append(t)
        log.info("stream manager started", extra={"status": self.worker_id})

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self.workers.values())
            self.workers.clear()
        for w in workers:
            w.stop("worker shutting down")
        for t in self._threads:
            t.join(timeout=3)
        self._threads.clear()

    # ------------------------------------------------------------------ reconcile
    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile()
            except Exception:
                log.exception("reconcile failed")
            self._stop.wait(self.reconcile_interval)

    def reconcile(self) -> None:
        with session_scope() as db:
            cams = db.query(Camera).all()
            desired = {c.id: (camera_worker_config(c), config_signature(c)) for c in cams if c.enabled and owns_camera(c.id, self.shard)}
            known = {c.id for c in cams}
            ocr_thr = float(settings_cache.section(db, "ocr")["confidence_threshold"])
        with self._lock:
            for cid in list(self.workers):
                if cid not in desired:
                    self._stop_worker(cid, "stopped by operator" if cid in known else "camera deleted")
            for cid, (cfg, sig) in desired.items():
                w = self.workers.get(cid)
                if w is not None and self.signatures.get(cid) == sig and w.is_running():
                    continue
                if w is not None:
                    self._stop_worker(cid, "configuration changed; restarting")
                self._start_worker(cfg, sig, ocr_thr)

    def _start_worker(self, cfg: dict[str, Any], sig: str, ocr_thr: float) -> None:
        try:
            assert self.registry is not None
            w = StreamWorker(cfg, self.registry, self.bus, evidence_writer=get_evidence_store(), spool=self.spool, ocr_threshold=ocr_thr,
                             bucket_seconds=int((cfg.get("processing") or {}).get("bucket_seconds", 60)))
            w.start()
            self.workers[cfg["id"]] = w
            self.signatures[cfg["id"]] = sig
            log.info("camera worker started", extra={"camera_id": cfg["id"]})
        except Exception:
            log.exception("could not start camera worker", extra={"camera_id": cfg["id"]})

    def _stop_worker(self, cid: str, reason: str) -> None:
        w = self.workers.pop(cid, None)
        self.signatures.pop(cid, None)
        if w is not None:
            try:
                w.stop(reason)
            except Exception:
                log.exception("worker stop failed", extra={"camera_id": cid})

    # ------------------------------------------------------------------ control
    def _control_loop(self) -> None:
        while not self._stop.is_set():
            try:
                for cmd in self.bus.read_control(timeout=0.5):
                    self.handle(cmd)
            except Exception:
                log.exception("control loop error")
                self._stop.wait(1.0)

    def handle(self, cmd: dict[str, Any]) -> dict[str, Any]:
        action, cid = cmd.get("cmd"), cmd.get("camera_id")
        with self._lock:
            w = self.workers.get(cid) if cid else None
            if action in ("start", "reload", "restart"):
                if action == "restart" and cid:
                    self._stop_worker(cid, "restart requested")
                self.reconcile()
                return {"ok": True}
            if action == "stop" and cid:
                self._stop_worker(cid, "stopped by operator")
                return {"ok": True}
            if action == "fault" and w is not None:
                w.inject_fault(str(cmd.get("kind")), float(cmd.get("seconds", 30)))
                return {"ok": True}
            if action == "clear_faults" and w is not None:
                w.source.clear_faults()
                return {"ok": True}
        return {"ok": False, "error": "camera not running on this worker" if cid else "unknown command"}

    # ------------------------------------------------------------------ heartbeat / spool
    def status(self) -> dict[str, Any]:
        with self._lock:
            running = {cid: w.health.status for cid, w in self.workers.items()}
        return {
            "worker_id": self.worker_id, "shard": f"{self.shard[0]}/{self.shard[1]}", "uptime_s": round(time.time() - self.started_at, 1), "cameras": running,
            "spool_depth": self.spool.depth(), "spooled": self.spool.spooled, "replayed": self.spool.replayed,
            "models": self.registry.status() if self.registry else [], "bus": self.bus.backend,
            "evidence_written": get_evidence_store().written, "evidence_skipped_disk_full": get_evidence_store().skipped_full,
        }

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.spool.depth():
                    n = self.spool.replay(self.bus.publish_ingest)
                    if n:
                        log.info("replayed %d spooled events", n)
                info = self.status()
                with session_scope() as db:
                    hb = db.get(WorkerHeartbeat, self.worker_id)
                    if hb is None:
                        db.add(WorkerHeartbeat(worker_id=self.worker_id, ts=datetime.now(timezone.utc), info=info))
                    else:
                        hb.ts = datetime.now(timezone.utc)
                        hb.info = info
            except Exception:
                log.exception("heartbeat failed")
            self._stop.wait(10.0)
