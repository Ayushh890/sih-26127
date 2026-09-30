"""Local dead-letter spool for events the worker could not hand to the bus.

If Redis is unreachable or the ingest queue is full, observations are appended to a
JSON-lines file instead of being lost; the stream manager re-publishes them when the
bus recovers. Events that keep failing stay in the spool and are visible on /system.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import orjson

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger("spool")


class Spool:
    def __init__(self, path: Path | None = None, max_bytes: int = 256 * 1024 * 1024) -> None:
        self.path = path or (get_settings().DATA_DIR / "deadletter" / f"spool-{get_settings().WORKER_ID}.jsonl")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()
        self.spooled = 0
        self.replayed = 0
        self.dropped = 0

    def put(self, event: dict[str, Any]) -> None:
        with self._lock:
            try:
                if self.path.exists() and self.path.stat().st_size > self.max_bytes:
                    self.dropped += 1
                    log.error("dead-letter spool full; event dropped", extra={"camera_id": event.get("camera_id")})
                    return
                with self.path.open("ab") as f:
                    f.write(orjson.dumps(event, option=orjson.OPT_SERIALIZE_NUMPY, default=str) + b"\n")
                self.spooled += 1
            except OSError:
                self.dropped += 1
                log.exception("could not write dead-letter spool")

    def depth(self) -> int:
        with self._lock:
            if not self.path.exists():
                return 0
            with self.path.open("rb") as f:
                return sum(1 for _ in f)

    def replay(self, publish) -> int:  # type: ignore[no-untyped-def]
        """Re-publish spooled events with ``publish(event) -> bool``; keeps those that still fail."""
        with self._lock:
            if not self.path.exists() or self.path.stat().st_size == 0:
                return 0
            lines = self.path.read_bytes().splitlines()
            remaining: list[bytes] = []
            sent = 0
            for i, line in enumerate(lines):
                try:
                    ev = orjson.loads(line)
                except orjson.JSONDecodeError:
                    continue
                if publish(ev):
                    sent += 1
                else:
                    remaining = lines[i:]
                    break
            self.path.write_bytes(b"\n".join(remaining) + (b"\n" if remaining else b""))
            self.replayed += sent
            return sent
