"""End-to-end smoke run of the real pipeline on the synthetic demo network.

Seeds an isolated SQLite database, restarts the demo timeline, runs the stream workers,
the ingestion service and the scheduler for ``--seconds`` and prints what was produced.

    python scripts/smoke_demo.py --seconds 260 --db /tmp/nirnay-smoke.db
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=260)
    ap.add_argument("--db", default="/tmp/nirnay-smoke.db")
    ap.add_argument("--start-at", type=float, default=0.0, help="scenario second to start the demo timeline at")
    ap.add_argument("--cameras", default="", help="comma separated subset of demo cameras to enable")
    a = ap.parse_args()
    for suffix in ("", "-wal", "-shm"):
        Path(a.db + suffix).unlink(missing_ok=True)
    os.environ["DATABASE_URL"] = f"sqlite:///{a.db}"
    os.environ.setdefault("DATA_DIR", "/tmp/nirnay-smoke-data")

    from sqlalchemy import func, select

    from app.core.bus import InMemoryBus
    from app.db.models import Alert, Camera, DeadLetter, GlobalVehicle, TrafficMetric, TrajectoryPoint, VehicleObservation
    from app.db.seed import seed_all
    from app.db.session import create_schema, session_scope
    from app.services import demo_control
    from app.services.ingestion import IngestionService
    from app.services.scheduler import Scheduler, analytics_snapshot
    from app.workers.manager import StreamManager

    create_schema()
    with session_scope() as db:
        print("seed:", seed_all(db))
        if a.cameras:
            keep = set(a.cameras.split(","))
            for c in db.scalars(select(Camera)):
                c.enabled = c.id in keep
        db.commit()
        demo_control.restart_timeline(db)
        if a.start_at:
            for c in db.scalars(select(Camera).where(Camera.is_demo.is_(True))):
                opts = dict((c.processing or {}).get("source_options") or {})
                opts["epoch_offset_s"] = time.time() - a.start_at
                c.processing = {**(c.processing or {}), "source_options": opts}
            db.commit()

    bus = InMemoryBus()
    ingest = IngestionService(bus, "smoke")
    sched = Scheduler(bus, rules_every=10, analytics_every=10)
    mgr = StreamManager(bus)
    ingest.start()
    sched.start()
    mgr.start()
    t0 = time.time()
    try:
        while time.time() - t0 < a.seconds:
            time.sleep(20)
            with session_scope() as db:
                obs = db.scalar(select(func.count(VehicleObservation.id)))
                alerts = db.scalar(select(func.count(Alert.id)))
            st = mgr.status()
            print(f"t={time.time() - t0:5.0f}s observations={obs} alerts={alerts} ingest_depth={bus.ingest_depth()} workers={len(st.get('workers', st))}",
                  flush=True)
    finally:
        mgr.stop()
        time.sleep(2)
        ingest.drain_once(0.5)
        ingest.stop()
        sched.stop()

    with session_scope() as db:
        print("\n== cameras")
        for c in db.scalars(select(Camera).order_by(Camera.id)):
            n = db.scalar(select(func.count(VehicleObservation.id)).where(VehicleObservation.camera_id == c.id))
            p = db.scalar(select(func.count(VehicleObservation.id)).where(VehicleObservation.camera_id == c.id, VehicleObservation.plate_text.is_not(None)))
            print(f"{c.id} status={c.status} observations={n} plates={p}")
        print("\n== vehicles seen at >1 camera")
        for v in db.scalars(select(GlobalVehicle).where(GlobalVehicle.camera_count > 1).order_by(GlobalVehicle.camera_count.desc()).limit(15)):
            pts = db.scalar(select(func.count(TrajectoryPoint.id)).where(TrajectoryPoint.global_vehicle_id == v.id))
            print(f"{v.code} plate={v.plate_text} cameras={v.camera_count} obs={v.observation_count} points={pts}")
        print("\n== match confidence levels")
        print(dict(db.execute(select(VehicleObservation.match_confidence_level, func.count()).group_by(VehicleObservation.match_confidence_level)).all()))
        print("\n== alerts")
        for al in db.scalars(select(Alert).order_by(Alert.id)):
            print(f"{al.code} {al.type} {al.status} {al.severity} cam={al.camera_id} {al.title}")
        print("\n== traffic metric rows:", db.scalar(select(func.count(TrafficMetric.id))), " dead letters:", db.scalar(select(func.count(DeadLetter.id))))
        snap = analytics_snapshot(db)
        print("== demo scope:", {k: v for k, v in snap["scopes"].get("demo", {}).items() if k != "totals"})
        for c in snap["cameras"]:
            print(f"  {c['camera_id']} {c['level']} {c['score']}")
        print("\n== alert explanations")
        for al in db.scalars(select(Alert).where(Alert.type.not_in(["CAMERA_DEGRADED", "CAMERA_OFFLINE"])).order_by(Alert.id)):
            print(f"{al.code} {al.type}: {al.reason}")
        print("\n== timeline")
        for e in demo_control.timeline(db)["events"][:10]:
            print(" ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
