"""Event bus abstraction.

Channels
--------
* ``ingest``  – durable queue from stream workers to the ingestion service
  (Redis Stream + consumer group; bounded in-memory queue in dev mode).
* ``ui``      – fan-out of real-time events to WebSocket clients (Redis Pub/Sub).
* ``control`` – commands from the API to stream workers (start/stop/fault-injection).
* frames      – latest raw/annotated preview JPEG per camera (Redis keys with TTL).
* runtime     – latest live processing metrics per camera (Redis hash).
* input frames – bounded per-camera inbox of JPEGs pushed *into* the system by the API
  (browser Local Camera uploads) and consumed by that camera's stream worker
  (Redis list trimmed to ``INPUT_FRAMES_MAX``; oldest frames are dropped).

All publish methods are thread-safe and never raise: a failing bus must not
take down a camera worker. Failures are logged and counted.
"""
from __future__ import annotations

import asyncio
import queue
import struct
import threading
import time
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import orjson

from app.core.logging import get_logger

log = get_logger("bus")

INGEST_STREAM = "nirnay:ingest"
INGEST_GROUP = "ingestors"
UI_CHANNEL = "nirnay:ui"
CONTROL_CHANNEL = "nirnay:control"
RUNTIME_HASH = "nirnay:runtime"
INPUT_FRAMES_MAX = 3  # per camera: a slow worker drops old uploads instead of growing memory
_TS = struct.Struct("<d")


def ui_event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": event_type, "ts": datetime.now(timezone.utc).isoformat(), "data": data}


def _dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_SERIALIZE_NUMPY, default=str)


class EventBus(ABC):
    backend: str = "abstract"

    def __init__(self) -> None:
        self.publish_failures = 0

    # ingest ------------------------------------------------------------------
    @abstractmethod
    def publish_ingest(self, event: dict[str, Any], timeout: float = 2.0) -> bool: ...

    @abstractmethod
    def read_ingest(self, consumer: str, max_count: int = 50, block_s: float = 1.0) -> list[tuple[str, dict[str, Any]]]: ...

    @abstractmethod
    def ack_ingest(self, ids: list[str]) -> None: ...

    @abstractmethod
    def ingest_depth(self) -> int: ...

    # ui ----------------------------------------------------------------------
    @abstractmethod
    def publish_ui(self, event: dict[str, Any]) -> None: ...

    @abstractmethod
    def ui_listener(self) -> AsyncIterator[dict[str, Any]]: ...

    # control -----------------------------------------------------------------
    @abstractmethod
    def publish_control(self, command: dict[str, Any]) -> None: ...

    @abstractmethod
    def read_control(self, timeout: float = 0.5) -> list[dict[str, Any]]: ...

    # frames / runtime ------------------------------------------------------------
    @abstractmethod
    def put_frame(self, camera_id: str, kind: str, jpeg: bytes) -> None: ...

    @abstractmethod
    def get_frame(self, camera_id: str, kind: str) -> tuple[bytes, float] | None: ...

    @abstractmethod
    def put_runtime(self, camera_id: str, state: dict[str, Any]) -> None: ...

    @abstractmethod
    def remove_runtime(self, camera_id: str) -> None: ...

    @abstractmethod
    def get_runtime(self) -> dict[str, dict[str, Any]]: ...

    # input frames (pushed by the API, consumed by a stream worker) ----------------------
    @abstractmethod
    def push_input_frame(self, camera_id: str, jpeg: bytes, ts: float | None = None) -> bool:
        """Queue an uploaded frame; returns ``False`` if it could not be queued."""

    @abstractmethod
    def pop_input_frame(self, camera_id: str, timeout: float = 1.0) -> tuple[bytes, float] | None:
        """Oldest queued upload and its capture timestamp, or ``None`` after ``timeout``."""

    @abstractmethod
    def clear_input_frames(self, camera_id: str) -> None: ...

    @abstractmethod
    def ping(self) -> bool: ...

    def close(self) -> None:  # pragma: no cover - optional
        pass


# ============================================================================ in-memory
class InMemoryBus(EventBus):
    """Single-process bus used in development (``WORKER_MODE=embedded`` without Redis)."""

    backend = "memory"

    def __init__(self, max_ingest: int = 20000) -> None:
        super().__init__()
        self._ingest: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue(maxsize=max_ingest)
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._control: queue.Queue[dict[str, Any]] = queue.Queue()
        self._subs: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()
        self._subs_lock = threading.Lock()
        self._frames: dict[tuple[str, str], tuple[bytes, float]] = {}
        self._runtime: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._inputs: dict[str, deque[tuple[bytes, float]]] = {}
        self._inputs_cond = threading.Condition()
        self.input_frames_dropped = 0

    def publish_ingest(self, event: dict[str, Any], timeout: float = 2.0) -> bool:
        with self._seq_lock:
            self._seq += 1
            eid = str(self._seq)
        try:
            # serialise exactly like the Redis transport so dev mode sees identical payloads
            self._ingest.put((eid, orjson.loads(_dumps(event))), timeout=timeout)  # blocking put = backpressure
            return True
        except queue.Full:
            self.publish_failures += 1
            log.warning("ingest queue full; event %s dropped to dead-letter", event.get("type"))
            return False

    def read_ingest(self, consumer: str, max_count: int = 50, block_s: float = 1.0) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        try:
            out.append(self._ingest.get(timeout=block_s))
        except queue.Empty:
            return out
        while len(out) < max_count:
            try:
                out.append(self._ingest.get_nowait())
            except queue.Empty:
                break
        return out

    def ack_ingest(self, ids: list[str]) -> None:
        return None

    def ingest_depth(self) -> int:
        return self._ingest.qsize()

    def publish_ui(self, event: dict[str, Any]) -> None:
        with self._subs_lock:
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, event)
            except RuntimeError:  # loop closed
                with self._subs_lock:
                    self._subs.discard((loop, q))

    @staticmethod
    def _offer(q: asyncio.Queue[dict[str, Any]], event: dict[str, Any]) -> None:
        if q.full():  # slow consumer: drop oldest to keep latency bounded
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                pass
        q.put_nowait(event)

    async def ui_listener(self) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=1000)
        key = (loop, q)
        with self._subs_lock:
            self._subs.add(key)
        try:
            while True:
                yield await q.get()
        finally:
            with self._subs_lock:
                self._subs.discard(key)

    def publish_control(self, command: dict[str, Any]) -> None:
        self._control.put(command)

    def read_control(self, timeout: float = 0.5) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        try:
            out.append(self._control.get(timeout=timeout))
            while True:
                out.append(self._control.get_nowait())
        except queue.Empty:
            pass
        return out

    def put_frame(self, camera_id: str, kind: str, jpeg: bytes) -> None:
        with self._lock:
            self._frames[(camera_id, kind)] = (jpeg, time.time())

    def get_frame(self, camera_id: str, kind: str) -> tuple[bytes, float] | None:
        with self._lock:
            item = self._frames.get((camera_id, kind))
        if item and time.time() - item[1] > 15:
            return None
        return item

    def put_runtime(self, camera_id: str, state: dict[str, Any]) -> None:
        with self._lock:
            self._runtime[camera_id] = state

    def remove_runtime(self, camera_id: str) -> None:
        with self._lock:
            self._runtime.pop(camera_id, None)
            for kind in ("raw", "annotated"):
                self._frames.pop((camera_id, kind), None)

    def get_runtime(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._runtime.items()}

    def push_input_frame(self, camera_id: str, jpeg: bytes, ts: float | None = None) -> bool:
        with self._inputs_cond:
            q = self._inputs.setdefault(camera_id, deque(maxlen=INPUT_FRAMES_MAX))
            if len(q) == q.maxlen:
                self.input_frames_dropped += 1
            q.append((jpeg, ts or time.time()))
            self._inputs_cond.notify_all()
        return True

    def pop_input_frame(self, camera_id: str, timeout: float = 1.0) -> tuple[bytes, float] | None:
        deadline = time.time() + timeout
        with self._inputs_cond:
            while True:
                q = self._inputs.get(camera_id)
                if q:
                    return q.popleft()
                left = deadline - time.time()
                if left <= 0:
                    return None
                self._inputs_cond.wait(left)

    def clear_input_frames(self, camera_id: str) -> None:
        with self._inputs_cond:
            self._inputs.pop(camera_id, None)

    def ping(self) -> bool:
        return True


# ============================================================================ redis
class RedisBus(EventBus):
    backend = "redis"

    def __init__(self, url: str, max_stream_len: int = 100000) -> None:
        super().__init__()
        import redis

        self.url = url
        self.max_stream_len = max_stream_len
        self._r = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=3, health_check_interval=15)
        self._control_pubsub: Any = None
        self._group_ready = False

    def _ensure_group(self) -> None:
        if self._group_ready:
            return
        import redis

        try:
            self._r.xgroup_create(INGEST_STREAM, INGEST_GROUP, id="0", mkstream=True)
        except redis.ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._group_ready = True

    def publish_ingest(self, event: dict[str, Any], timeout: float = 2.0) -> bool:
        deadline = time.time() + timeout
        while True:
            try:
                self._r.xadd(INGEST_STREAM, {"e": _dumps(event)}, maxlen=self.max_stream_len, approximate=True)
                return True
            except Exception as exc:  # redis unavailable: retry until timeout
                if time.time() >= deadline:
                    self.publish_failures += 1
                    log.warning("ingest publish failed: %s", exc)
                    return False
                time.sleep(0.2)

    def read_ingest(self, consumer: str, max_count: int = 50, block_s: float = 1.0) -> list[tuple[str, dict[str, Any]]]:
        self._ensure_group()
        out: list[tuple[str, dict[str, Any]]] = []
        # first reclaim messages another consumer left pending for too long (crash recovery)
        try:
            claimed = self._r.xautoclaim(INGEST_STREAM, INGEST_GROUP, consumer, min_idle_time=60000, start_id="0-0", count=max_count)
            for mid, fields in claimed[1]:
                if fields:
                    out.append((mid.decode(), orjson.loads(fields[b"e"])))
        except Exception:
            pass
        if out:
            return out
        resp = self._r.xreadgroup(INGEST_GROUP, consumer, {INGEST_STREAM: ">"}, count=max_count, block=int(block_s * 1000))
        for _stream, messages in resp or []:
            for mid, fields in messages:
                out.append((mid.decode(), orjson.loads(fields[b"e"])))
        return out

    def ack_ingest(self, ids: list[str]) -> None:
        if ids:
            self._r.xack(INGEST_STREAM, INGEST_GROUP, *ids)
            self._r.xdel(INGEST_STREAM, *ids)

    def ingest_depth(self) -> int:
        try:
            return int(self._r.xlen(INGEST_STREAM))
        except Exception:
            return -1

    def publish_ui(self, event: dict[str, Any]) -> None:
        try:
            self._r.publish(UI_CHANNEL, _dumps(event))
        except Exception as exc:
            self.publish_failures += 1
            log.warning("ui publish failed: %s", exc)

    async def ui_listener(self) -> AsyncIterator[dict[str, Any]]:  # type: ignore[override]
        import redis.asyncio as aioredis

        while True:
            client = aioredis.Redis.from_url(self.url)
            pubsub = client.pubsub()
            try:
                await pubsub.subscribe(UI_CHANNEL)
                async for msg in pubsub.listen():
                    if msg.get("type") == "message":
                        yield orjson.loads(msg["data"])
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("ui listener lost redis connection: %s; retrying", exc)
                await asyncio.sleep(2)
            finally:
                try:
                    await pubsub.aclose()
                    await client.aclose()
                except Exception:
                    pass

    def publish_control(self, command: dict[str, Any]) -> None:
        try:
            self._r.publish(CONTROL_CHANNEL, _dumps(command))
        except Exception as exc:
            log.warning("control publish failed: %s", exc)

    def read_control(self, timeout: float = 0.5) -> list[dict[str, Any]]:
        try:
            if self._control_pubsub is None:
                self._control_pubsub = self._r.pubsub(ignore_subscribe_messages=True)
                self._control_pubsub.subscribe(CONTROL_CHANNEL)
            out: list[dict[str, Any]] = []
            msg = self._control_pubsub.get_message(timeout=timeout)
            while msg:
                if msg.get("type") == "message":
                    out.append(orjson.loads(msg["data"]))
                msg = self._control_pubsub.get_message(timeout=0)
            return out
        except Exception as exc:
            log.warning("control read failed: %s", exc)
            self._control_pubsub = None
            time.sleep(timeout)
            return []

    def put_frame(self, camera_id: str, kind: str, jpeg: bytes) -> None:
        try:
            pipe = self._r.pipeline()
            pipe.set(f"nirnay:frame:{camera_id}:{kind}", jpeg, ex=15)
            pipe.set(f"nirnay:frame:{camera_id}:{kind}:ts", str(time.time()), ex=15)
            pipe.execute()
        except Exception:
            self.publish_failures += 1

    def get_frame(self, camera_id: str, kind: str) -> tuple[bytes, float] | None:
        try:
            jpeg, ts = self._r.mget(f"nirnay:frame:{camera_id}:{kind}", f"nirnay:frame:{camera_id}:{kind}:ts")
        except Exception:
            return None
        if jpeg is None:
            return None
        return jpeg, float(ts or 0)

    def put_runtime(self, camera_id: str, state: dict[str, Any]) -> None:
        try:
            self._r.hset(RUNTIME_HASH, camera_id, _dumps(state))
        except Exception:
            self.publish_failures += 1

    def remove_runtime(self, camera_id: str) -> None:
        try:
            self._r.hdel(RUNTIME_HASH, camera_id)
        except Exception:
            pass

    def get_runtime(self) -> dict[str, dict[str, Any]]:
        try:
            raw = self._r.hgetall(RUNTIME_HASH)
        except Exception:
            return {}
        out: dict[str, dict[str, Any]] = {}
        now = time.time()
        for k, v in raw.items():
            state = orjson.loads(v)
            if now - float(state.get("updated_at") or 0) < 30:  # ignore state from dead workers
                out[k.decode()] = state
        return out

    def push_input_frame(self, camera_id: str, jpeg: bytes, ts: float | None = None) -> bool:
        key = f"nirnay:input:{camera_id}"
        try:
            pipe = self._r.pipeline()
            pipe.lpush(key, _TS.pack(ts or time.time()) + jpeg)
            pipe.ltrim(key, 0, INPUT_FRAMES_MAX - 1)  # newest N kept: the worker never falls behind by more than N frames
            pipe.expire(key, 15)
            pipe.execute()
            return True
        except Exception:
            self.publish_failures += 1
            return False

    def pop_input_frame(self, camera_id: str, timeout: float = 1.0) -> tuple[bytes, float] | None:
        try:
            got = self._r.brpop([f"nirnay:input:{camera_id}"], timeout=max(0.1, timeout))
        except Exception:
            time.sleep(min(timeout, 1.0))
            return None
        if not got:
            return None
        blob = got[1]
        return blob[_TS.size:], _TS.unpack_from(blob)[0]

    def clear_input_frames(self, camera_id: str) -> None:
        try:
            self._r.delete(f"nirnay:input:{camera_id}")
        except Exception:
            pass

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            return False

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass


# ============================================================================ factory
_bus: EventBus | None = None
_bus_lock = threading.Lock()


def get_bus() -> EventBus:
    global _bus
    with _bus_lock:
        if _bus is None:
            from app.core.config import get_settings

            url = get_settings().REDIS_URL
            _bus = RedisBus(url) if url else InMemoryBus()
            log.info("event bus initialised (backend=%s)", _bus.backend)
        return _bus


def set_bus(bus: EventBus | None) -> None:
    """Override the process bus (tests)."""
    global _bus
    with _bus_lock:
        _bus = bus
