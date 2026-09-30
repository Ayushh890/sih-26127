"""Camera topology graph: directed camera-to-camera edges with distances and travel times.

Used to reject physically impossible matches (a vehicle cannot cover the shortest
road distance faster than ``min_travel_s``), to score route plausibility (direct
neighbours vs. skipped cameras), for next-camera prediction and for corridor routing.
Edges with ``allowed = False`` (e.g. the wrong direction of a one-way road) are
never traversed.
"""
from __future__ import annotations

import heapq
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Camera, CameraEdge


@dataclass(frozen=True)
class EdgeInfo:
    from_id: str
    to_id: str
    distance_m: float
    min_travel_s: float
    typical_travel_s: float
    allowed: bool
    road_name: str
    path: list | None = None


@dataclass
class PathInfo:
    cameras: list[str]
    distance_m: float
    min_travel_s: float
    typical_travel_s: float
    edges: list[EdgeInfo] = field(default_factory=list)

    @property
    def hops(self) -> int:
        return len(self.cameras) - 1

    @property
    def skipped(self) -> list[str]:
        return self.cameras[1:-1]

    def as_dict(self) -> dict[str, Any]:
        return {"cameras": self.cameras, "distance_m": round(self.distance_m, 1), "min_travel_s": round(self.min_travel_s, 1),
                "typical_travel_s": round(self.typical_travel_s, 1), "hops": self.hops}


class TopologyGraph:
    def __init__(self, edges: list[EdgeInfo], cameras: dict[str, dict[str, Any]]) -> None:
        self.cameras = cameras
        self.edges = edges
        self.adj: dict[str, list[EdgeInfo]] = {}
        self.radj: dict[str, list[EdgeInfo]] = {}
        self.by_pair: dict[tuple[str, str], EdgeInfo] = {}
        for e in edges:
            self.by_pair[(e.from_id, e.to_id)] = e
            if e.allowed:
                self.adj.setdefault(e.from_id, []).append(e)
                self.radj.setdefault(e.to_id, []).append(e)
        self._cache: dict[tuple[str, str, str], PathInfo | None] = {}

    def edge(self, a: str, b: str) -> EdgeInfo | None:
        return self.by_pair.get((a, b))

    def outgoing(self, a: str) -> list[EdgeInfo]:
        return self.adj.get(a, [])

    def incoming(self, b: str) -> list[EdgeInfo]:
        return self.radj.get(b, [])

    def shortest(self, a: str, b: str, weight: str = "distance_m", travel_times: dict[tuple[str, str], float] | None = None) -> PathInfo | None:
        """Shortest allowed path from ``a`` to ``b``; for ``a == b`` the shortest loop back."""
        key = (a, b, weight)
        if travel_times is None and key in self._cache:
            return self._cache[key]
        res = self._dijkstra(a, b, weight, travel_times)
        if travel_times is None:
            self._cache[key] = res
        return res

    def _w(self, e: EdgeInfo, weight: str, tt: dict[tuple[str, str], float] | None) -> float:
        if tt is not None and (e.from_id, e.to_id) in tt:
            return tt[(e.from_id, e.to_id)]
        return float(getattr(e, weight))

    def _dijkstra(self, a: str, b: str, weight: str, tt: dict[tuple[str, str], float] | None) -> PathInfo | None:
        if a not in self.cameras or b not in self.cameras:
            return None
        # a loop (a == b) must leave a first: seed the queue with a's outgoing edges
        dist: dict[str, float] = {}
        prev: dict[str, EdgeInfo] = {}
        heap: list[tuple[float, str]] = []
        if a == b:
            for e in self.outgoing(a):
                w = self._w(e, weight, tt)
                if e.to_id not in dist or w < dist[e.to_id]:
                    dist[e.to_id], prev[e.to_id] = w, e
                    heapq.heappush(heap, (w, e.to_id))
        else:
            dist[a] = 0.0
            heap = [(0.0, a)]
        done: set[str] = set()
        target_cost: float | None = None
        loop_edge: EdgeInfo | None = None
        while heap:
            d, u = heapq.heappop(heap)
            if u in done:
                continue
            done.add(u)
            if a != b and u == b:
                break
            for e in self.outgoing(u):
                nd = d + self._w(e, weight, tt)
                if a == b and e.to_id == a:
                    if target_cost is None or nd < target_cost:
                        target_cost, loop_edge = nd, e
                    continue
                if e.to_id not in dist or nd < dist[e.to_id]:
                    dist[e.to_id], prev[e.to_id] = nd, e
                    heapq.heappush(heap, (nd, e.to_id))
        chain: list[EdgeInfo] = []
        if a == b:
            if loop_edge is None:
                return None
            chain.append(loop_edge)
            node = loop_edge.from_id
            while node != a:
                e = prev[node]
                chain.append(e)
                node = e.from_id
        else:
            if b not in dist:
                return None
            node = b
            while node != a:
                e = prev[node]
                chain.append(e)
                node = e.from_id
        chain.reverse()
        cams = [a] + [e.to_id for e in chain]
        return PathInfo(cams, sum(e.distance_m for e in chain), sum(e.min_travel_s for e in chain),
                        sum(e.typical_travel_s for e in chain), chain)

    def geojson(self) -> dict[str, Any]:
        feats = []
        for e in self.edges:
            coords = [[p[1], p[0]] for p in (e.path or [])] or [
                [self.cameras[e.from_id]["lon"], self.cameras[e.from_id]["lat"]], [self.cameras[e.to_id]["lon"], self.cameras[e.to_id]["lat"]]]
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": coords},
                          "properties": {"from": e.from_id, "to": e.to_id, "allowed": e.allowed, "road_name": e.road_name,
                                         "distance_m": e.distance_m, "min_travel_s": e.min_travel_s, "typical_travel_s": e.typical_travel_s}})
        return {"type": "FeatureCollection", "features": feats}


_lock = threading.Lock()
_graph: TopologyGraph | None = None
_loaded = 0.0
TTL_S = 30.0


def build_graph(db: Session) -> TopologyGraph:
    cams = {c.id: {"lat": c.latitude, "lon": c.longitude, "name": c.name, "road_name": c.road_name, "is_demo": c.is_demo,
                   "direction": c.direction} for c in db.query(Camera).all()}
    edges = []
    for e in db.query(CameraEdge).all():
        if e.from_camera_id not in cams or e.to_camera_id not in cams:
            continue
        path = None
        if e.path_geojson and e.path_geojson.get("coordinates"):
            path = [[c[1], c[0]] for c in e.path_geojson["coordinates"]]
        edges.append(EdgeInfo(e.from_camera_id, e.to_camera_id, e.distance_m, e.min_travel_s, e.typical_travel_s, e.allowed, e.road_name, path))
    return TopologyGraph(edges, cams)


def get_topology(db: Session) -> TopologyGraph:
    global _graph, _loaded
    with _lock:
        if _graph is not None and time.time() - _loaded < TTL_S:
            return _graph
    g = build_graph(db)
    with _lock:
        _graph, _loaded = g, time.time()
    return g


def invalidate_topology() -> None:
    global _graph
    with _lock:
        _graph = None
