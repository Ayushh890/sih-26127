"""Ingestion service: the single writer that turns worker events into database state.

Consumes the durable ``ingest`` channel (Redis Stream consumer group, or the in-memory
queue in dev mode). Each event is processed in its own transaction and acknowledged
only after commit. Failing events are retried; after ``max_attempts`` they are moved to
the ``dead_letters`` table (and acknowledged) so one bad event cannot block the stream.

Event types
-----------
* ``observation``    – finalised single-camera track → track, observation, plate reads,
  embedding, evidence → global identity → trajectory → alerts → UI events
* ``traffic_sample`` – per-minute frame statistics → ``traffic_metrics`` (merged per bucket)
* ``health_sample``  – camera health snapshot → ``camera_health``
* ``camera_status``  – status transition → camera row, system event, offline alerts
"""
from __future__ import annotations

import base64
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.bus import EventBus, ui_event
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import plate_hash
from app.db.models import (
    Camera,
    CameraHealth,
    DeadLetter,
    Evidence,
    PlateRead,
    SystemEvent,
    TrafficMetric,
    VehicleEmbedding,
    VehicleObservation,
    VehicleTrack,
)
from app.db.session import session_scope
from app.evidence.store import get_evidence_store
from app.ml.ocr.fusion import MIN_PLATE_CHARS
from app.services import alert_rules, trajectory
from app.services.alerts import AlertEngine
from app.services.identity import IdentityResolver, decode_embedding
from app.services.settings_service import settings_cache
from app.services.topology import get_topology
from app.services.travel_times import baselines

log = get_logger("ingestion")

FRAGMENT_MERGE_S = 25.0  # same plate at the same camera within this gap = one passage split by the tracker


def _ts(v: Any) -> datetime:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), tz=timezone.utc)
    return datetime.fromisoformat(str(v)).astimezone(timezone.utc)


def observation_summary(obs: VehicleObservation, cam: Camera | None, vehicle_code: str | None) -> dict[str, Any]:
    return {
        "observation_id": obs.id, "camera_id": obs.camera_id, "camera_name": cam.name if cam else None, "observed_at": obs.observed_at.isoformat(),
        "plate_text": obs.plate_text, "plate_confidence": obs.plate_confidence, "vehicle_class": obs.vehicle_class, "vehicle_color": obs.vehicle_color,
        "speed_kmh": obs.speed_kmh, "direction": obs.direction, "motion": obs.motion, "lane": obs.lane,
        "global_vehicle_id": obs.global_vehicle_id, "vehicle_code": vehicle_code, "match_score": obs.match_score,
        "confidence_level": obs.match_confidence_level, "match_reasons": obs.match_reasons, "evidence_id": obs.evidence_id, "is_demo": obs.is_demo,
    }


class IngestionService:
    def __init__(self, bus: EventBus, consumer: str | None = None, max_attempts: int = 3, batch: int = 50) -> None:
        self.bus = bus
        self.consumer = consumer or f"ingest-{get_settings().WORKER_ID}"
        self.max_attempts = max_attempts
        self.batch = batch
        self.alerts = AlertEngine(bus)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempts: dict[str, int] = {}
        self.stats = {"processed": 0, "failed": 0, "dead_lettered": 0, "duplicates": 0, "merged_fragments": 0, "last_event_at": None,
                      "last_latency_ms": None}

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="ingestion", daemon=True)
        self._thread.start()
        log.info("ingestion started (consumer %s)", self.consumer)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once(block_s=1.0)
            except Exception:  # bus outage: back off, never die
                log.exception("ingestion loop error")
                time.sleep(2.0)

    def drain_once(self, block_s: float = 0.2) -> int:
        """Process one batch from the bus; returns the number of events handled."""
        msgs = self.bus.read_ingest(self.consumer, self.batch, block_s)
        done: list[str] = []
        for mid, ev in msgs:
            if self.process(ev, message_id=mid):
                done.append(mid)
        if done:
            self.bus.ack_ingest(done)
        return len(msgs)

    # ------------------------------------------------------------------ processing
    def process(self, ev: dict[str, Any], message_id: str | None = None) -> bool:
        """Handle one event with retries. Returns True when it can be acknowledged
        (processed, duplicate or dead-lettered)."""
        key = message_id or str(ev.get("event_id") or id(ev))
        t0 = time.perf_counter()
        for attempt in range(1, self.max_attempts + 1):
            try:
                with session_scope() as db:
                    after = self._dispatch(db, ev)
                for fn in after:  # UI notifications only after a successful commit
                    fn()
                self.stats["processed"] += 1
                self.stats["last_event_at"] = datetime.now(timezone.utc).isoformat()
                self.stats["last_latency_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                self._attempts.pop(key, None)
                return True
            except Exception as exc:
                self.stats["failed"] += 1
                log.warning("ingest event %s failed (attempt %d/%d): %s", ev.get("type"), attempt, self.max_attempts, exc,
                            extra={"camera_id": ev.get("camera_id")})
                if attempt >= self.max_attempts:
                    self._dead_letter(ev, exc, attempt)
                    return True
                time.sleep(0.05 * attempt)
        return True

    def _dead_letter(self, ev: dict[str, Any], exc: Exception, attempts: int) -> None:
        self.stats["dead_lettered"] += 1
        # keep dead letters small and always JSON-safe (a non-serialisable payload must not lose the event)
        payload = orjson.loads(orjson.dumps({k: v for k, v in ev.items() if k != "embedding"}, option=orjson.OPT_SERIALIZE_NUMPY, default=str))
        try:
            with session_scope() as db:
                db.add(DeadLetter(source="ingest", event_type=str(ev.get("type"))[:32], camera_id=ev.get("camera_id"), payload=payload,
                                  error="".join(traceback.format_exception_only(type(exc), exc)).strip()[:4000], attempts=attempts))
                db.add(SystemEvent(level="ERROR", source="ingestion", camera_id=ev.get("camera_id"), event_type="dead_letter",
                                   message=f"{ev.get('type')} event moved to dead-letter queue: {exc}"[:2000], details={"attempts": attempts}))
        except Exception:
            log.exception("could not write dead letter")

    def replay_dead_letter(self, db: Session, dl: DeadLetter) -> bool:
        ok = False
        try:
            with session_scope() as s2:
                after = self._dispatch(s2, dict(dl.payload))
            for fn in after:
                fn()
            ok = True
        except Exception as exc:
            dl.attempts += 1
            dl.error = f"replay failed: {exc}"[:4000]
        if ok:
            dl.resolved, dl.resolved_at, dl.resolution = True, datetime.now(timezone.utc), "replayed"
        db.commit()
        return ok

    def _dispatch(self, db: Session, ev: dict[str, Any]) -> list:
        typ = ev.get("type")
        if typ == "observation":
            return self._observation(db, ev)
        if typ == "traffic_sample":
            return self._traffic(db, ev)
        if typ == "health_sample":
            return self._health(db, ev)
        if typ == "camera_status":
            return self._status(db, ev)
        raise ValueError(f"unknown ingest event type {typ!r}")

    # ------------------------------------------------------------------ observation
    def _observation(self, db: Session, ev: dict[str, Any]) -> list:
        event_id = str(ev["event_id"])
        if db.scalar(select(VehicleObservation.id).where(VehicleObservation.event_id == event_id)) is not None:
            self.stats["duplicates"] += 1
            return []
        cam = db.get(Camera, ev["camera_id"])
        if cam is None:
            log.warning("observation for unknown camera %s dropped", ev["camera_id"])
            self._discard_evidence(ev)
            return []
        observed = _ts(ev["observed_at"])
        plate = ev.get("plate_text")
        if plate and len(plate) < MIN_PLATE_CHARS:  # fragment from an external producer: keep the raw text only
            plate = None

        # tracker fragments: the same plate at the same camera moments apart is one passage
        if plate:
            frag = db.scalar(select(VehicleObservation).where(
                VehicleObservation.camera_id == cam.id, VehicleObservation.plate_text == plate,
                VehicleObservation.observed_at >= observed - timedelta(seconds=FRAGMENT_MERGE_S),
                VehicleObservation.observed_at <= observed + timedelta(seconds=FRAGMENT_MERGE_S)).limit(1))
            if frag is not None:
                self._merge_fragment(db, frag, ev)
                self.stats["merged_fragments"] += 1
                return []

        track = VehicleTrack(camera_id=cam.id, local_track_id=int(ev["local_track_id"]), started_at=_ts(ev["first_seen"]), ended_at=_ts(ev["last_seen"]),
                             vehicle_class=ev["vehicle_class"], class_confidence=float(ev.get("class_confidence") or 0), frames=int(ev.get("frames") or 0),
                             path=ev.get("path") or [])
        db.add(track)
        db.flush()
        evd = ev.get("evidence")
        obs = VehicleObservation(
            event_id=event_id, camera_id=cam.id, track_id=track.id, local_track_id=int(ev["local_track_id"]), observed_at=observed,
            first_seen_at=_ts(ev["first_seen"]), last_seen_at=_ts(ev["last_seen"]), plate_text=plate, plate_raw=ev.get("plate_raw"),
            plate_confidence=ev.get("plate_confidence") if plate else None, plate_valid=bool(plate and ev.get("plate_valid")), plate_hash=plate_hash(plate),
            plate_votes=int(ev.get("plate_votes") or 0), vehicle_class=ev["vehicle_class"], class_confidence=float(ev.get("class_confidence") or 0),
            vehicle_color=ev.get("vehicle_color"), color_rgb=ev.get("color_rgb"), bbox=ev.get("bbox"), speed_kmh=ev.get("speed_kmh"),
            direction=ev.get("direction"), heading_deg=ev.get("heading_deg"), motion=ev.get("motion"), lane=ev.get("lane"),
            evidence_id=evd["id"] if evd else None, is_demo=bool(cam.is_demo))
        db.add(obs)
        db.flush()
        self._plate_reads(db, obs, ev)
        if evd:
            db.add(Evidence(id=evd["id"], observation_id=obs.id, camera_id=cam.id, ts=observed, files=evd.get("files") or {}, bbox=evd.get("bbox"),
                            plate_bbox=evd.get("plate_bbox"), ocr_text=evd.get("ocr_text"), ocr_confidence=evd.get("ocr_confidence"),
                            manifest_sha256=evd["manifest_sha256"], encrypted=bool(evd.get("encrypted"))))
        emb = decode_embedding(ev.get("embedding"))
        model = ev.get("embedding_model")
        if emb is not None and model:
            db.add(VehicleEmbedding(observation_id=obs.id, model=model, dim=int(emb.shape[0]), vector=emb.astype(np.float32).tobytes()))
        db.flush()

        # --- identity & trajectory ----------------------------------------------------
        graph = get_topology(db)
        icfg = settings_cache.section(db, "identity")
        ccfg = settings_cache.section(db, "congestion")
        base = baselines(db, graph, ccfg["baseline_s"], 0)
        match = IdentityResolver(db, graph, icfg, base).resolve(obs, emb, model)
        vehicle = match.vehicle or trajectory.create_vehicle(db, obs)
        obs.global_vehicle_id = vehicle.id
        obs.previous_observation_id = match.prev.id if match.prev is not None else None
        obs.match_score = match.score
        obs.match_confidence_level = match.confidence_level
        obs.match_reasons = match.reasons
        obs.match_components = {**match.components, "considered": match.considered, "new_journey": match.new_journey,
                                "conflicts": match.conflicts}
        point = trajectory.append(db, vehicle, obs, match, cam, graph)
        raised = alert_rules.on_observation(self.alerts, db, settings_cache.section(db, "alerts"), obs, cam, vehicle, match)
        db.flush()

        summary = observation_summary(obs, cam, vehicle.code)
        prediction = trajectory.prediction_state(vehicle)
        point_d = {"global_vehicle_id": vehicle.id, "vehicle_code": vehicle.code, "seq": point.seq, "journey_index": point.journey_index,
                   "camera_id": point.camera_id, "from_camera_id": point.from_camera_id, "ts": point.ts.isoformat(), "latitude": point.latitude,
                   "longitude": point.longitude, "link_score": point.link_score, "confidence_level": point.confidence_level,
                   "segment_travel_s": point.segment_travel_s, "segment_speed_kmh": point.segment_speed_kmh,
                   "prediction_status": point.prediction_status, "explanation": point.explanation, "prediction": prediction,
                   "plate_text": vehicle.plate_text, "is_demo": vehicle.is_demo}
        log.info("observation %s at %s → %s (%s, score %s, %d alerts)", obs.id, cam.id, vehicle.code, match.confidence_level, match.score,
                 len(raised), extra={"camera_id": cam.id})
        bus = self.bus
        return [lambda: bus.publish_ui(ui_event("vehicle_matched", {**summary, "sensitive": True, "prediction": prediction,
                                                                    "new_vehicle": match.vehicle is None})),
                lambda: bus.publish_ui(ui_event("trajectory_updated", {**point_d, "sensitive": True}))]

    def _plate_reads(self, db: Session, obs: VehicleObservation, ev: dict[str, Any]) -> None:
        for r in ev.get("plate_reads") or []:
            db.add(PlateRead(observation_id=obs.id, camera_id=obs.camera_id, local_track_id=obs.local_track_id, ts=_ts(r["ts"]),
                             raw_text=str(r.get("raw_text") or "")[:32], normalized_text=str(r.get("normalized_text") or "")[:16],
                             confidence=float(r.get("confidence") or 0), char_confidences=r.get("char_confidences"),
                             is_valid_format=bool(r.get("is_valid")), corrections=r.get("corrections"), plate_bbox=r.get("plate_bbox")))

    def _merge_fragment(self, db: Session, obs: VehicleObservation, ev: dict[str, Any]) -> None:
        self._plate_reads(db, obs, ev)
        obs.last_seen_at = max(obs.last_seen_at, _ts(ev["last_seen"]))
        obs.first_seen_at = min(obs.first_seen_at, _ts(ev["first_seen"]))
        obs.plate_votes = (obs.plate_votes or 0) + int(ev.get("plate_votes") or 0)
        if (ev.get("plate_confidence") or 0) > (obs.plate_confidence or 0):
            obs.plate_confidence = ev.get("plate_confidence")
        if obs.speed_kmh is None and ev.get("speed_kmh") is not None:
            obs.speed_kmh = ev.get("speed_kmh")
        if obs.track_id:
            t = db.get(VehicleTrack, obs.track_id)
            if t is not None:
                t.frames += int(ev.get("frames") or 0)
                t.ended_at = max(t.ended_at, _ts(ev["last_seen"]))
        self._discard_evidence(ev)  # the first fragment's evidence already covers this passage

    @staticmethod
    def _discard_evidence(ev: dict[str, Any]) -> None:
        evd = ev.get("evidence")
        if evd and evd.get("files"):
            try:
                get_evidence_store().delete(evd["files"])
            except Exception:
                log.exception("could not delete discarded evidence")

    # ------------------------------------------------------------------ metrics / health / status
    def _traffic(self, db: Session, ev: dict[str, Any]) -> list:
        cam = db.get(Camera, ev["camera_id"])
        if cam is None:
            return []
        bucket = _ts(ev["bucket_start"])
        row = db.scalar(select(TrafficMetric).where(TrafficMetric.camera_id == cam.id, TrafficMetric.bucket_start == bucket))
        frames = int(ev.get("frames") or 0)
        if row is None:
            db.add(TrafficMetric(camera_id=cam.id, bucket_start=bucket, bucket_seconds=int(ev.get("bucket_seconds") or 60), frames=frames,
                                 new_tracks=int(ev.get("new_tracks") or 0), avg_vehicles_in_frame=float(ev.get("avg_vehicles_in_frame") or 0),
                                 max_vehicles_in_frame=int(ev.get("max_vehicles_in_frame") or 0), occupancy=float(ev.get("occupancy") or 0),
                                 avg_stationary=float(ev.get("avg_stationary") or 0), avg_speed_kmh=ev.get("avg_speed_kmh"), is_demo=cam.is_demo))
            return []
        # a worker restart mid-bucket: merge (frame-weighted)
        tot = row.frames + frames

        def wmean(a: float, b: float) -> float:
            return (a * row.frames + b * frames) / tot if tot else 0.0

        row.avg_vehicles_in_frame = wmean(row.avg_vehicles_in_frame, float(ev.get("avg_vehicles_in_frame") or 0))
        row.occupancy = wmean(row.occupancy, float(ev.get("occupancy") or 0))
        row.avg_stationary = wmean(row.avg_stationary, float(ev.get("avg_stationary") or 0))
        if ev.get("avg_speed_kmh") is not None:
            row.avg_speed_kmh = ev["avg_speed_kmh"] if row.avg_speed_kmh is None else (row.avg_speed_kmh + ev["avg_speed_kmh"]) / 2
        row.frames = tot
        row.new_tracks += int(ev.get("new_tracks") or 0)
        row.max_vehicles_in_frame = max(row.max_vehicles_in_frame, int(ev.get("max_vehicles_in_frame") or 0))
        row.bucket_seconds = max(row.bucket_seconds, int(ev.get("bucket_seconds") or 60))
        return []

    def _health(self, db: Session, ev: dict[str, Any]) -> list:
        cam = db.get(Camera, ev["camera_id"])
        if cam is None:
            return []
        ts = _ts(ev["ts"])
        db.add(CameraHealth(camera_id=cam.id, ts=ts, status=ev.get("status") or cam.status, input_fps=float(ev.get("input_fps") or 0),
                            processing_fps=float(ev.get("processing_fps") or 0), latency_ms=float(ev.get("latency_ms") or 0),
                            inference_ms=float(ev.get("inference_ms") or 0), queue_depth=int(ev.get("queue_depth") or 0),
                            frames_processed=int(ev.get("frames_processed") or 0), frames_dropped=int(ev.get("frames_dropped") or 0),
                            vehicles_detected=int(ev.get("vehicles_detected") or 0), plates_read=int(ev.get("plates_read") or 0),
                            avg_ocr_confidence=ev.get("avg_ocr_confidence"), blur_score=ev.get("blur_score"), brightness=ev.get("brightness"),
                            reconnects=int(ev.get("reconnects") or 0), error=ev.get("error")))
        if ev.get("status") in ("ONLINE", "DEGRADED") and (ev.get("input_fps") or 0) > 0:
            cam.last_seen_at = ts
        return []

    def _status(self, db: Session, ev: dict[str, Any]) -> list:
        cam = db.get(Camera, ev["camera_id"])
        if cam is None:
            return []
        status, msg = str(ev["status"]), str(ev.get("message") or "")
        cam.status, cam.status_message = status, msg[:2000]
        if status == "ONLINE":
            cam.last_seen_at = _ts(ev.get("ts") or time.time())
        intentional = bool(ev.get("intentional"))
        level = "INFO" if intentional else {"ONLINE": "INFO", "CONNECTING": "INFO", "DEGRADED": "WARNING", "OFFLINE": "ERROR", "ERROR": "ERROR"}.get(status, "INFO")
        db.add(SystemEvent(ts=_ts(ev.get("ts") or time.time()), level=level, source="stream_worker", camera_id=cam.id, event_type="camera_status",
                           message=f"{ev.get('old_status')} → {status}: {msg}"[:2000],
                           details={"old": ev.get("old_status"), "new": status, "intentional": intentional}))
        if not intentional:
            alert_rules.on_camera_status(self.alerts, db, settings_cache.section(db, "alerts"), cam, status, msg)
        return []


_service: IngestionService | None = None


def get_ingestion() -> IngestionService | None:
    return _service


def set_ingestion(svc: IngestionService | None) -> None:
    global _service
    _service = svc


def encode_embedding(vec: np.ndarray) -> str:
    return base64.b64encode(vec.astype(np.float32).tobytes()).decode()
