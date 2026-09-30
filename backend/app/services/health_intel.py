"""Camera health intelligence: a 0–100 health score per camera with the evidence behind
it and concrete maintenance recommendations, computed from recorded ``camera_health``
samples (and the live runtime snapshot when the worker is running).
"""
from __future__ import annotations

import statistics
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Camera, CameraHealth


def camera_health_report(db: Session, cam: Camera, runtime: dict[str, Any] | None, window_s: float = 3600, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    rows = list(db.scalars(select(CameraHealth).where(CameraHealth.camera_id == cam.id, CameraHealth.ts >= now - timedelta(seconds=window_s))
                           .order_by(CameraHealth.ts)))
    proc = cam.processing or {}
    target_fps = float(proc.get("processing_fps") or 0) or None
    out: dict[str, Any] = {"camera_id": cam.id, "camera_name": cam.name, "status": cam.status, "status_message": cam.status_message,
                           "window_s": window_s, "samples": len(rows), "enabled": cam.enabled}
    if not rows and not runtime:
        return {**out, "score": None, "grade": "NO_DATA", "factors": [], "recommendations":
                (["Camera processing is disabled — enable it to collect health data."] if not cam.enabled else
                 ["No health samples yet — samples are recorded every 15 s while the camera is processed."]), "trend": []}

    factors: list[dict[str, Any]] = []
    recs: list[str] = []
    penalty = 0.0

    def add(name: str, value: Any, impact: float, text: str, rec: str | None = None) -> None:
        nonlocal penalty
        penalty += impact
        factors.append({"factor": name, "value": value, "impact": round(-impact, 1), "text": text})
        if rec and rec not in recs:
            recs.append(rec)

    if rows:
        up = sum(1 for r in rows if r.status in ("ONLINE", "DEGRADED")) / len(rows)
        if up < 0.99:
            add("availability", round(up, 3), (1 - up) * 40, f"available in {up:.0%} of samples",
                "Check power, network link and stream credentials; review reconnect history in camera logs.")
        reconnects = max(r.reconnects for r in rows) - min(r.reconnects for r in rows)
        if reconnects > 0:
            add("reconnects", reconnects, min(15, reconnects * 3), f"{reconnects} reconnect(s) in the window",
                "Intermittent stream: check cabling/PoE, switch port errors and the camera's bitrate/GOP settings.")
        in_fps = [r.input_fps for r in rows if r.input_fps > 0]
        proc_fps = [r.processing_fps for r in rows if r.processing_fps > 0]
        if target_fps and proc_fps and statistics.median(proc_fps) < 0.7 * target_fps:
            m = statistics.median(proc_fps)
            infer = statistics.median([r.inference_ms for r in rows if r.inference_ms > 0] or [0])
            add("processing_fps", round(m, 1), 10, f"processing {m:.1f} fps vs target {target_fps:.0f} fps (inference {infer:.0f} ms)",
                "Processing is saturated: lower processing_fps/resolution for this camera, raise frame_skip, or move it to a GPU worker.")
        if in_fps and statistics.median(in_fps) < 5:
            add("input_fps", round(statistics.median(in_fps), 1), 8, f"camera delivers only {statistics.median(in_fps):.1f} fps",
                "Increase the camera's stream frame rate or use its main stream instead of a low-rate sub-stream.")
        blur = [r.blur_score for r in rows if r.blur_score is not None]
        if len(blur) >= 4:
            early, late = statistics.median(blur[: len(blur) // 2]), statistics.median(blur[len(blur) // 2:])
            if early > 0 and late < 0.5 * early:
                add("sharpness", round(late, 1), 15, f"image sharpness fell from {early:.0f} to {late:.0f} (Laplacian variance)",
                    "Image is getting blurry: clean the lens/housing, check for condensation, and re-focus.")
        bright = [r.brightness for r in rows if r.brightness is not None]
        if bright and statistics.median(bright[-4:]) < 40:
            add("brightness", round(statistics.median(bright[-4:]), 1), 10, f"scene brightness {statistics.median(bright[-4:]):.0f}/255",
                "Scene is too dark for plate reading: add IR illumination or adjust exposure/shutter settings.")
        veh = sum(r.vehicles_detected for r in rows)
        plates = sum(r.plates_read for r in rows)
        if veh >= 10:
            rate = plates / veh
            if rate < 0.4:
                add("plate_read_rate", round(rate, 3), 15 * (1 - rate / 0.4), f"plates read for {rate:.0%} of {veh} vehicles",
                    "Low plate capture: zoom in or re-aim so plates are ≥ 100 px wide, and reduce shutter time to avoid motion blur.")
        ocr = [(r.avg_ocr_confidence, r.plates_read) for r in rows if r.avg_ocr_confidence is not None and r.plates_read]
        if ocr:
            mean = sum(c * n for c, n in ocr) / sum(n for _, n in ocr)
            if mean < 0.7:
                add("ocr_confidence", round(mean, 3), (0.7 - mean) * 40, f"mean OCR confidence {mean:.0%}",
                    "OCR confidence is low: check focus, glare on plates and the camera angle (keep it under ~30°).")
        dropped = sum(r.frames_dropped for r in rows[-1:])
        if dropped and rows[-1].frames_processed and dropped / max(1, rows[-1].frames_processed) > 0.5:
            add("frame_drops", dropped, 5, f"{dropped} frames dropped by the input queue",
                "Frames are dropped before processing: raise max_queue_size or lower the processing load.")
    if runtime and runtime.get("status") in ("OFFLINE", "ERROR"):
        add("current_status", runtime.get("status"), 30, f"currently {runtime.get('status')}: {runtime.get('last_error') or ''}",
            "Camera is not delivering frames right now: verify reachability (ping/RTSP) and credentials, then use Test connection.")
    score = max(0.0, 100.0 - penalty)
    grade = "GOOD" if score >= 85 else "FAIR" if score >= 60 else "POOR"
    trend = [{"ts": r.ts.isoformat(), "status": r.status, "input_fps": r.input_fps, "processing_fps": r.processing_fps, "blur": r.blur_score,
              "brightness": r.brightness, "ocr": r.avg_ocr_confidence, "vehicles": r.vehicles_detected, "plates": r.plates_read} for r in rows[-240:]]
    if not recs:
        recs.append("No issues detected in the window.")
    return {**out, "score": round(score, 1), "grade": grade, "factors": factors, "recommendations": recs, "trend": trend}
