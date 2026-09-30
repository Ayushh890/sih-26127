"""NIRNAY API application.

``uvicorn app.main:app`` starts the REST/WebSocket API. With ``WORKER_MODE=embedded`` (the
default for single-machine and development use) the same process also runs the camera
stream workers, the ingestion consumer and the scheduler; with ``external`` those run in
``python -m app.workers.main`` and communicate through Redis.
"""
from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.api.routes import alerts, analytics, auth, cameras, demo, evidence, local_camera, onvif, system, topology, users, vehicles, watchlist, ws
from app.core.bus import get_bus
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger, request_id_var
from app.core.ratelimit import SlidingWindowLimiter
from app.core.runtime import runtime
from app.core.security import sha256_bytes
from app.db.session import db_latency_ms, session_scope

settings = get_settings()
configure_logging(settings.LOG_LEVEL, settings.LOG_JSON)
log = get_logger("api")

API_DESCRIPTION = """
City-wide multi-camera ANPR, vehicle trajectory and traffic-intelligence platform (SIH26127).

**Authorised use only.** This system is intended for authorised traffic-management
environments. It processes vehicles and traffic only; it performs no facial recognition
and does not identify people. It never controls traffic signals (the emergency corridor is
a simulation). Plate text is pseudonymised for roles without `plates:view_raw`.

Authenticate with `POST /api/auth/login` and send `Authorization: Bearer <token>`.
"""

TAGS = [
    {"name": "auth", "description": "Login, current user, password change"},
    {"name": "cameras", "description": "Camera management, control, health, snapshots and MJPEG streams"},
    {"name": "vehicles", "description": "Plate search, sightings, global identities and trajectories"},
    {"name": "analytics", "description": "Aggregate traffic analytics"},
    {"name": "alerts", "description": "Rule-based alerts and acknowledgement workflow"},
    {"name": "system", "description": "System status, dead letters, audit log and settings"},
]

api_limiter = SlidingWindowLimiter(settings.RATE_LIMIT_PER_MINUTE, 60.0)
RATE_EXEMPT = ("/health", "/ready", "/docs", "/openapi.json", "/redoc")


def _start_services() -> None:
    from app.db.migrate import init_database
    from app.db.seed import seed_all

    problems = settings.validate_production()
    if problems:
        for p in problems:
            log.error("configuration problem: %s", p)
        raise RuntimeError("refusing to start in production with insecure configuration: " + "; ".join(problems))
    method = init_database()
    with session_scope() as db:
        seeded = seed_all(db)
    log.info("database ready (%s); seed: %s", method, {k: len(v) if isinstance(v, list) else v for k, v in seeded.items()})
    runtime.bus = get_bus()
    runtime.mode = settings.WORKER_MODE
    if settings.WORKER_MODE == "external" and runtime.bus.backend == "memory":
        msg = "WORKER_MODE=external needs REDIS_URL; the in-memory bus cannot reach another process"
        runtime.startup_errors.append(msg)
        log.error(msg)
    if settings.WORKER_MODE == "embedded":
        from app.services.ingestion import IngestionService
        from app.services.scheduler import Scheduler
        from app.workers.manager import StreamManager

        runtime.ingestion = IngestionService(runtime.bus)
        runtime.scheduler = Scheduler(runtime.bus)
        runtime.ingestion.start()
        runtime.scheduler.start()
        try:
            runtime.manager = StreamManager(runtime.bus)
            runtime.manager.start()
        except Exception as exc:  # models missing etc.: API stays up and reports the problem
            runtime.startup_errors.append(f"stream manager failed to start: {exc}")
            log.exception("stream manager failed to start")


def _stop_services() -> None:
    for name in ("manager", "ingestion", "scheduler"):
        svc = getattr(runtime, name)
        if svc is not None:
            try:
                svc.stop()
            except Exception:
                log.exception("error stopping %s", name)
            setattr(runtime, name, None)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    from starlette.concurrency import run_in_threadpool

    runtime.started_at = time.time()
    await run_in_threadpool(_start_services)
    log.info("NIRNAY API started (mode=%s, bus=%s)", runtime.mode, runtime.bus.backend if runtime.bus else None)
    try:
        yield
    finally:
        await run_in_threadpool(_stop_services)
        log.info("NIRNAY API stopped")


def create_app(start_services: bool = True) -> FastAPI:
    app = FastAPI(title="NIRNAY API", version="1.0.0", description=API_DESCRIPTION, openapi_tags=TAGS,
                  lifespan=lifespan if start_services else None)
    app.add_middleware(CORSMiddleware, allow_origins=settings.CORS_ORIGINS, allow_credentials=True,
                       allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"], allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
                       expose_headers=["X-Request-ID", "Retry-After", "X-RateLimit-Remaining", "X-Evidence-SHA256"])

    @app.middleware("http")
    async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
        rid = request.headers.get("x-request-id", "")[:64] or uuid.uuid4().hex[:16]
        token = request_id_var.set(rid)
        t0 = time.perf_counter()
        try:
            path = request.url.path
            remaining = None
            if path.startswith(("/api", "/ws")) and not path.startswith(RATE_EXEMPT):
                auth = request.headers.get("authorization", "")
                # key by credential when present (users behind one NAT get separate budgets), otherwise by client address
                key = "t:" + sha256_bytes(auth.encode())[:16] if auth else "ip:" + (request.client.host if request.client else "?")
                ok, remaining, retry = api_limiter.check(key)
                if not ok:
                    return JSONResponse({"detail": "rate limit exceeded"}, status.HTTP_429_TOO_MANY_REQUESTS,
                                        headers={"Retry-After": str(int(retry) + 1), "X-Request-ID": rid})
            response = await call_next(request)
            response.headers["X-Request-ID"] = rid
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            if remaining is not None:
                response.headers["X-RateLimit-Remaining"] = str(remaining)
            ms = (time.perf_counter() - t0) * 1000
            if path.startswith("/api") and not path.endswith((".mjpg", ".jpg")):
                log.info("%s %s %s", request.method, path, response.status_code,
                         extra={"latency_ms": round(ms, 1), "status": response.status_code, "user": getattr(request.state, "username", None)})
            return response
        finally:
            request_id_var.reset(token)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # never echo submitted values back (they may contain passwords or stream credentials)
        errors = [{"loc": e.get("loc"), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return JSONResponse({"detail": errors}, status.HTTP_422_UNPROCESSABLE_CONTENT)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"detail": "internal server error", "request_id": request_id_var.get()}, status.HTTP_500_INTERNAL_SERVER_ERROR)

    @app.get("/health", tags=["system"], summary="Liveness: the API process is up")
    def health() -> dict[str, Any]:
        return {"status": "ok", "service": "nirnay-api", "uptime_s": round(time.time() - runtime.started_at, 1)}

    @app.get("/ready", tags=["system"], summary="Readiness: database, event bus and processing services")
    def ready() -> JSONResponse:
        from app.db.models import Camera, WorkerHeartbeat

        checks: dict[str, Any] = {}
        lat = db_latency_ms()
        checks["database"] = {"ok": lat is not None, "latency_ms": lat}
        bus = runtime.bus
        checks["bus"] = {"ok": bool(bus and bus.ping()), "backend": bus.backend if bus else None}
        if runtime.mode == "embedded":
            checks["ingestion"] = {"ok": bool(runtime.ingestion and runtime.ingestion.is_running())}
            checks["scheduler"] = {"ok": bool(runtime.scheduler and runtime.scheduler.is_running())}
            checks["stream_manager"] = {"ok": runtime.manager is not None}
        if lat is not None:
            try:
                with session_scope() as db:
                    checks["cameras"] = {"ok": True, "configured": db.scalar(select(func.count()).select_from(Camera)) or 0}
                    hb = db.scalar(select(func.max(WorkerHeartbeat.ts)))
                    checks["workers"] = {"ok": True, "last_heartbeat": hb.isoformat() if hb else None}
            except Exception as exc:
                checks["cameras"] = {"ok": False, "error": str(exc)[:200]}
        ok = all(c["ok"] for c in checks.values()) and not runtime.startup_errors
        return JSONResponse({"status": "ready" if ok else "not_ready", "mode": runtime.mode, "checks": checks, "startup_errors": runtime.startup_errors},
                            status.HTTP_200_OK if ok else status.HTTP_503_SERVICE_UNAVAILABLE)

    for r in (auth, users, cameras, vehicles, topology, analytics, alerts, watchlist, evidence, system, onvif, demo, ws, local_camera):
        app.include_router(r.router)
    return app


app = create_app()
