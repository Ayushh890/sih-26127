"""Processing worker process for ``WORKER_MODE=external`` (Docker Compose ``worker`` service).

    python -m app.workers.main                 # camera workers + ingestion + scheduler
    python -m app.workers.main --role streams  # camera workers only (scale out per camera group)
    python -m app.workers.main --role ingest   # ingestion + scheduler only

The API process owns schema migration and seeding; this process waits for the schema,
then talks to the API exclusively through Redis (ingest stream, UI pub/sub, control
channel, frame/runtime keys) and the shared database. Run exactly one scheduler.
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from sqlalchemy import text

from app.core.bus import get_bus
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.db.session import session_scope

log = get_logger("worker.main")


def wait_for_schema(timeout_s: float) -> None:
    deadline = time.time() + timeout_s
    delay = 1.0
    while True:
        try:
            with session_scope() as db:
                db.execute(text("SELECT 1 FROM cameras LIMIT 1"))
                db.execute(text("SELECT 1 FROM system_settings LIMIT 1"))
            return
        except Exception as exc:
            if time.time() > deadline:
                raise RuntimeError(f"database schema not available after {timeout_s:.0f}s: {exc}") from exc
            log.info("waiting for the API to create the database schema (%s)", type(exc).__name__)
            time.sleep(delay)
            delay = min(delay * 2, 10.0)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="NIRNAY processing worker")
    ap.add_argument("--role", choices=("all", "streams", "ingest"), default="all")
    ap.add_argument("--schema-timeout", type=float, default=180.0)
    a = ap.parse_args(argv)
    s = get_settings()
    configure_logging(s.LOG_LEVEL, s.LOG_JSON)
    problems = s.validate_production()
    if problems:
        log.error("refusing to start in production with insecure configuration: %s", "; ".join(problems))
        return 2
    bus = get_bus()
    if bus.backend == "memory":
        log.error("the worker process needs REDIS_URL: the in-memory bus cannot reach the API process")
        return 2
    wait_for_schema(a.schema_timeout)

    services: list[object] = []
    if a.role in ("all", "ingest"):
        from app.services.ingestion import IngestionService
        from app.services.scheduler import Scheduler

        services += [IngestionService(bus, s.WORKER_ID), Scheduler(bus)]
    if a.role in ("all", "streams"):
        from app.workers.manager import StreamManager

        services.append(StreamManager(bus))
    for svc in services:
        svc.start()  # type: ignore[attr-defined]
    log.info("worker %s started (role=%s, services=%s)", s.WORKER_ID, a.role, [type(x).__name__ for x in services])

    stop = threading.Event()

    def _term(signum: int, _frame: object) -> None:
        log.info("signal %d received: shutting down", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _term)
    signal.signal(signal.SIGINT, _term)
    stop.wait()
    for svc in reversed(services):
        try:
            svc.stop()  # type: ignore[attr-defined]
        except Exception:
            log.exception("error stopping %s", type(svc).__name__)
    log.info("worker %s stopped", s.WORKER_ID)
    return 0


if __name__ == "__main__":
    sys.exit(main())
