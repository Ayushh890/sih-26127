"""In-process sliding-window rate limiter (per client key).

Used as ASGI middleware for the whole API and separately for the login endpoint. With
several API replicas each replica enforces its own window, which bounds abuse per
instance; a shared limiter can be added at the reverse proxy.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_s: float = 60.0, max_keys: int = 50000) -> None:
        self.limit = limit
        self.window_s = window_s
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str, now: float | None = None) -> tuple[bool, int, float]:
        """Register a hit. Returns (allowed, remaining, retry_after_s)."""
        if self.limit <= 0:
            return True, 0, 0.0
        now = now or time.monotonic()
        with self._lock:
            q = self._hits.get(key)
            if q is None:
                if len(self._hits) >= self.max_keys:
                    self._evict(now)
                q = self._hits[key] = deque()
            while q and now - q[0] > self.window_s:
                q.popleft()
            if len(q) >= self.limit:
                return False, 0, round(self.window_s - (now - q[0]), 1)
            q.append(now)
            return True, self.limit - len(q), 0.0

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._hits.clear()
            else:
                self._hits.pop(key, None)

    def _evict(self, now: float) -> None:
        for k in [k for k, q in self._hits.items() if not q or now - q[-1] > self.window_s]:
            del self._hits[k]
