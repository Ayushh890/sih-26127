"""Background scheduler: periodic alert rules, the ``analytics_updated`` broadcast and
data retention. Runs in one thread; every job is isolated so a failure is logged and
recorded as a system event without stopping the others.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select

from app.core.bus import EventBus, ui_event
from app.core.logging import get_logger
from app.db.models import Alert, Camera, SystemEvent, VehicleObservation
from app.db.session import session_scope
from app.services import alert_rules, analytics, retention
from app.services.alerts import AlertEngine
from app.services.settings_service import settings_cache
from app.services.topology import get_topology

log = get_logger("scheduler")


def analytics_snapshot(db: Any, now: datetime | None = None) -> dict[str, Any]:
    """Aggregate-only network snapshot (no individual vehicles), split by data scope."""
    now = now or datetime.now(timezone.utc)
    ccfg = settings_cache.section(db, "congestion")
    graph = get_topology(db)
    cams = list(db.scalars(select(Camera).order_by(Camera.id)))
    cong = analytics.network_congestion(db, ccfg, graph, [c.id for c in cams], now)
    out: dict[str, Any] = {"generated_at": now.isoformat(), "window_s": ccfg["window_s"], "cameras": cong, "scopes": {}}
    for scope, flag in (("live", False), ("demo", True)):
        ids = [c.id for c in cams if c.is_demo == flag]
        if not ids:
            continue
        since = now - timedelta(seconds=ccfg["window_s"])
        tot = analytics.aggregate(analytics._metrics(db, ids, since, now))
        vehicles = db.scalar(select(func.count(func.distinct(VehicleObservation.global_vehicle_id))).where(
            VehicleObservation.camera_id.in_(ids), VehicleObservation.observed_at >= since)) or 0
        open_alerts = dict(db.execute(select(Alert.severity, func.count()).where(Alert.status != "RESOLVED", Alert.is_demo.is_(flag))
                                      .group_by(Alert.severity)).all())
        scored = [c for c in cong if c["camera_id"] in ids and c["score"] is not None]
        worst = max(scored, key=lambda c: c["score"]) if scored else None
        out["scopes"][scope] = {"cameras": len(ids), "online": sum(1 for c in cams if c.is_demo == flag and c.status in ("ONLINE", "DEGRADED")),
                                "totals": tot, "unique_vehicles": vehicles, "open_alerts": open_alerts,
                                "network_score": round(sum(c["score"] for c in scored) / len(scored), 1) if scored else None,
                                "worst_camera_id": worst["camera_id"] if worst else None}
    return out


class Scheduler:
    def __init__(self, bus: EventBus, rules_every: float = 15.0, analytics_every: float = 10.0, retention_every: float = 3600.0) -> None:
        self.bus = bus
        self.engine = AlertEngine(bus)
        self.jobs: list[tuple[str, float, Callable[[], None]]] = [
            ("periodic_rules", rules_every, self.run_rules),
            ("analytics_broadcast", analytics_every, self.broadcast_analytics),
            ("retention", retention_every, self.run_retention),
        ]
        self.last_run: dict[str, float] = {}
        self.last_error: dict[str, str | None] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        now = time.time()
        # first retention pass a minute after start-up, rules and analytics right away
        self.last_run = {"retention": now - 3600 + 60}
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(1.0):
            now = time.time()
            for name, every, fn in self.jobs:
                if now - self.last_run.get(name, 0.0) < every:
                    continue
                self.last_run[name] = now
                try:
                    fn()
                    self.last_error[name] = None
                except Exception as exc:
                    self.last_error[name] = str(exc)
                    log.exception("scheduled job %s failed", name)
                    try:
                        with session_scope() as db:
                            db.add(SystemEvent(level="ERROR", source="scheduler", event_type="job_failed", message=f"{name}: {exc}"[:2000]))
                    except Exception:
                        pass

    def status(self) -> dict[str, Any]:
        return {"running": self.is_running(), "jobs": [{"name": n, "every_s": e, "last_run": self.last_run.get(n), "last_error": self.last_error.get(n)}
                                                       for n, e, _ in self.jobs]}

    # ------------------------------------------------------------------ jobs
    def run_rules(self) -> int:
        with session_scope() as db:
            cams = list(db.scalars(select(Camera)))
            if not cams:
                return 0
            cfg = settings_cache.section(db, "alerts")
            ccfg = settings_cache.section(db, "congestion")
            graph = get_topology(db)
            return alert_rules.run_periodic(self.engine, db, cfg, ccfg, graph, cams)

    def broadcast_analytics(self) -> None:
        with session_scope() as db:
            snap = analytics_snapshot(db)
        self.bus.publish_ui(ui_event("analytics_updated", snap))

    def run_retention(self) -> dict[str, int]:
        with session_scope() as db:
            return retention.run(db)
