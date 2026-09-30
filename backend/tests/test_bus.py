"""Event bus transports: the Redis implementation (against fakeredis) must behave like the in-memory one."""
from __future__ import annotations

import time

import fakeredis
import orjson

from app.core.bus import RUNTIME_HASH, InMemoryBus, RedisBus


def redis_bus() -> RedisBus:
    b = RedisBus.__new__(RedisBus)
    b.publish_failures = 0
    b._r = fakeredis.FakeRedis()
    b._control_pubsub = None
    b._group_ready = False
    return b


def test_runtime_roundtrip_matches_memory_bus() -> None:
    state = {"camera_id": "CAM-X", "status": "ONLINE", "updated_at": time.time()}
    for bus in (InMemoryBus(), redis_bus()):
        bus.put_runtime("CAM-X", state)
        assert bus.get_runtime()["CAM-X"]["status"] == "ONLINE", bus.backend
        bus.remove_runtime("CAM-X")
        assert "CAM-X" not in bus.get_runtime()


def test_redis_runtime_ignores_state_from_dead_workers() -> None:
    bus = redis_bus()
    bus.put_runtime("FRESH", {"status": "ONLINE", "updated_at": time.time()})
    bus._r.hset(RUNTIME_HASH, "STALE", orjson.dumps({"status": "ONLINE", "updated_at": time.time() - 120}))
    assert set(bus.get_runtime()) == {"FRESH"}
