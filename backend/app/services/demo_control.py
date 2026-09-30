"""Demo scenario control: the scripted-event timeline and "restart demo timeline".

The demo cameras render a deterministic synthetic scenario whose clock is
``wall clock − epoch_offset_s`` (``processing.source_options.epoch_offset_s`` on each demo
camera; recorded demo videos are replayed wall-clock synchronised with the same offset). The timeline lists the *scheduled* scripted events of that scenario (what the
generator will render), so an operator can see what is coming; whether the platform
actually detects them is shown by the real observations and alerts.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db.models import Alert, Camera, CameraHealth, Evidence, GlobalVehicle, SystemEvent, TrafficMetric, VehicleObservation
from app.demo.network import load_network
from app.demo.scenario import get_scenario
from app.services import retention
from app.services.topology import invalidate_topology

EXPECTED = {
    "tracked": ("Tracked vehicle passes {cam}", "vehicle_matched / trajectory_updated (journey A→B→C…)"),
    "obscured_plate": ("Vehicle with unreadable plate passes {cam}", "linked by appearance only, with LOW/MEDIUM confidence"),
    "cloned_plate": ("Vehicle with plate {plate} passes {cam}", "IMPOSSIBLE_TRAVEL alert on the second sighting"),
    "wrong_way": ("Vehicle drives against a one-way at {cam}", "WRONG_WAY alert"),
    "circling": ("Vehicle {plate} passes {cam} again", "REPEATED_SIGHTING alert on the 3rd pass"),
}


def demo_cameras(db: Session) -> list[Camera]:
    return list(db.scalars(select(Camera).where(Camera.is_demo.is_(True), Camera.source_type.in_(("demo", "file"))).order_by(Camera.id)))


def current_offset(db: Session) -> float:
    cams = demo_cameras(db)
    if not cams:
        return 0.0
    return float(((cams[0].processing or {}).get("source_options") or {}).get("epoch_offset_s", 0.0))


def timeline(db: Session, now: float | None = None) -> dict[str, Any]:
    now = now or time.time()
    offset = current_offset(db)
    sc = get_scenario()
    net = load_network()
    L = sc.L
    st = now - offset
    k = int(st // L)
    events: list[dict[str, Any]] = []
    for kk in (k, k + 1):
        base = kk * L
        for j in sc.cycle(kk):
            if not j.scripted:
                continue
            for p in j.passes:
                title, expect = EXPECTED.get(j.scripted or "", ("{cam}", ""))
                events.append({"scenario": j.scripted, "camera_id": p.camera_id, "plate": j.plate,
                               "title": title.format(cam=p.camera_id, plate=j.plate or "(unreadable)"), "expected_outcome": expect,
                               "at": datetime.fromtimestamp(p.t + offset, tz=timezone.utc).isoformat(), "scenario_t": round(p.t - base, 1),
                               "cycle": kk})
        cong = net.get("scenario", {}).get("congestion")
        if cong:
            for edge, label in (("start", "Congestion builds at {cam}"), ("end", "Congestion clears at {cam}")):
                events.append({"scenario": "congestion", "camera_id": cong["camera_id"], "plate": None, "title": label.format(cam=cong["camera_id"]),
                               "expected_outcome": "congestion score rises (SEVERE_CONGESTION / TRAFFIC_SURGE / TRAVEL_TIME_ANOMALY)" if edge == "start"
                               else "congestion score falls; congestion alert auto-resolves",
                               "at": datetime.fromtimestamp(base + cong[edge] + offset, tz=timezone.utc).isoformat(), "scenario_t": cong[edge], "cycle": kk})
    for e in events:
        e["status"] = "done" if datetime.fromisoformat(e["at"]).timestamp() <= now else "upcoming"
    events.sort(key=lambda e: e["at"])
    return {"synthetic": True, "cycle_seconds": L, "cycle": k, "position_s": round(st - k * L, 1), "epoch_offset_s": offset,
            "cycle_started_at": datetime.fromtimestamp(k * L + offset, tz=timezone.utc).isoformat(), "events": events,
            "notice": "Scheduled events of the synthetic demo scenario (input script). Detections and alerts come from the real pipeline."}


def purge_demo_data(db: Session) -> dict[str, int]:
    """Delete all data produced by demo cameras (never touches live data)."""
    cam_ids = [c.id for c in db.scalars(select(Camera).where(Camera.is_demo.is_(True)))]
    out = {"evidence": retention.delete_evidence(db, Evidence.camera_id.in_(cam_ids)) if cam_ids else 0,
           "observations": retention.delete_observations(db, VehicleObservation.is_demo.is_(True))}
    out["vehicles"] = db.execute(delete(GlobalVehicle).where(GlobalVehicle.is_demo.is_(True))).rowcount or 0
    out["alerts"] = db.execute(delete(Alert).where(Alert.is_demo.is_(True))).rowcount or 0
    out["traffic_metrics"] = db.execute(delete(TrafficMetric).where(TrafficMetric.is_demo.is_(True))).rowcount or 0
    if cam_ids:
        out["camera_health"] = db.execute(delete(CameraHealth).where(CameraHealth.camera_id.in_(cam_ids))).rowcount or 0
    return out


def restart_timeline(db: Session, lead_s: float = 5.0) -> dict[str, Any]:
    """Purge demo-generated data and restart the scenario from its beginning.

    Scripted plates recur in every cycle, so replaying the story without purging would
    link the replay to the previous run (and could raise spurious alerts)."""
    purged = purge_demo_data(db)
    offset = round(time.time() - lead_s, 3)
    cams = demo_cameras(db)
    for c in cams:
        proc = dict(c.processing or {})
        proc["source_options"] = {**(proc.get("source_options") or {}), "epoch_offset_s": offset}
        c.processing = proc
    db.add(SystemEvent(level="INFO", source="demo", event_type="demo_restart", message=f"demo timeline restarted; purged {sum(purged.values())} demo rows",
                       details={"purged": purged, "epoch_offset_s": offset}))
    db.commit()
    invalidate_topology()
    return {"epoch_offset_s": offset, "purged": purged, "cameras": [c.id for c in cams]}
