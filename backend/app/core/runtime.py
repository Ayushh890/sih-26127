"""Process-wide handles to the long-running services (bus, stream manager, ingestion,
scheduler). Populated by the application lifespan (or the worker entry point) and read
by API routes; any handle may be ``None`` when that service runs in another process.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from app.core.bus import EventBus
    from app.services.ingestion import IngestionService
    from app.services.scheduler import Scheduler
    from app.workers.manager import StreamManager


@dataclass
class Runtime:
    bus: EventBus | None = None
    manager: StreamManager | None = None
    ingestion: IngestionService | None = None
    scheduler: Scheduler | None = None
    started_at: float = field(default_factory=time.time)
    mode: str = "api-only"
    startup_errors: list[str] = field(default_factory=list)

    def require_bus(self) -> EventBus:
        if self.bus is None:
            raise RuntimeError("event bus not initialised")
        return self.bus

    def describe(self) -> dict[str, Any]:
        return {"mode": self.mode, "uptime_s": round(time.time() - self.started_at, 1), "bus": self.bus.backend if self.bus else None,
                "stream_manager": self.manager is not None, "ingestion": bool(self.ingestion and self.ingestion.is_running()),
                "scheduler": bool(self.scheduler and self.scheduler.is_running()), "startup_errors": self.startup_errors}


runtime = Runtime()
