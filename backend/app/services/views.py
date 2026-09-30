"""Serialisers shared by REST endpoints and WebSocket payloads (plate masking is applied
by the caller according to the requesting user's role)."""
from __future__ import annotations

from typing import Any

from app.db.models import Evidence, GlobalVehicle, TrajectoryPoint, VehicleObservation
from app.ml.ocr.normalize import format_plate
from app.services.trajectory import prediction_state


def iso(dt: Any) -> str | None:
    return dt.isoformat() if dt is not None else None


def observation_dict(o: VehicleObservation, vehicle_code: str | None = None, camera_name: str | None = None) -> dict[str, Any]:
    return {
        "id": o.id, "camera_id": o.camera_id, "camera_name": camera_name, "observed_at": iso(o.observed_at),
        "first_seen_at": iso(o.first_seen_at), "last_seen_at": iso(o.last_seen_at),
        "plate_text": o.plate_text, "plate_display": format_plate(o.plate_text) if o.plate_text else None, "plate_raw": o.plate_raw,
        "plate_confidence": o.plate_confidence, "plate_valid": o.plate_valid, "plate_votes": o.plate_votes,
        "vehicle_class": o.vehicle_class, "class_confidence": o.class_confidence, "vehicle_color": o.vehicle_color,
        "speed_kmh": o.speed_kmh, "speed_is_estimate": o.speed_kmh is not None, "direction": o.direction, "heading_deg": o.heading_deg,
        "motion": o.motion, "lane": o.lane, "global_vehicle_id": o.global_vehicle_id, "vehicle_code": vehicle_code,
        "previous_observation_id": o.previous_observation_id, "match_score": o.match_score,
        "match_confidence_level": o.match_confidence_level, "match_reasons": o.match_reasons or [],
        "evidence_id": o.evidence_id, "is_demo": o.is_demo,
    }


def vehicle_dict(v: GlobalVehicle) -> dict[str, Any]:
    return {
        "id": v.id, "code": v.code, "plate_text": v.plate_text, "plate_display": format_plate(v.plate_text) if v.plate_text else None,
        "plate_confidence": v.plate_confidence, "vehicle_class": v.vehicle_class, "vehicle_color": v.vehicle_color,
        "first_seen_at": iso(v.first_seen_at), "last_seen_at": iso(v.last_seen_at), "first_camera_id": v.first_camera_id,
        "last_camera_id": v.last_camera_id, "observation_count": v.observation_count, "camera_count": v.camera_count,
        "total_distance_m": round(v.total_distance_m or 0.0, 1), "prediction": prediction_state(v), "is_demo": v.is_demo,
    }


def point_dict(p: TrajectoryPoint) -> dict[str, Any]:
    return {
        "id": p.id, "seq": p.seq, "journey_index": p.journey_index, "observation_id": p.observation_id, "camera_id": p.camera_id,
        "ts": iso(p.ts), "latitude": p.latitude, "longitude": p.longitude, "from_camera_id": p.from_camera_id,
        "segment_travel_s": p.segment_travel_s, "segment_distance_m": p.segment_distance_m, "segment_speed_kmh": p.segment_speed_kmh,
        "link_score": p.link_score, "confidence_level": p.confidence_level, "explanation": p.explanation,
        "prediction_status": p.prediction_status,
    }


def evidence_dict(e: Evidence) -> dict[str, Any]:
    return {
        "id": e.id, "observation_id": e.observation_id, "camera_id": e.camera_id, "ts": iso(e.ts),
        "files": {k: {kk: vv for kk, vv in (f or {}).items() if kk != "path"} for k, f in (e.files or {}).items()},
        "bbox": e.bbox, "plate_bbox": e.plate_bbox, "ocr_text": e.ocr_text, "ocr_confidence": e.ocr_confidence,
        "manifest_sha256": e.manifest_sha256, "encrypted": e.encrypted,
    }
