"""Demo network definition loader + geodesy helpers shared with the topology service."""
from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.config import get_settings

EARTH_R = 6371008.8


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(min(1.0, math.sqrt(a)))


def path_length_m(path: list[list[float]]) -> float:
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(path, path[1:]))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    x = math.sin(dl) * math.cos(p2)
    y = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(x, y)) + 360) % 360


def path_to_geojson(path: list[list[float]]) -> dict[str, Any]:
    return {"type": "LineString", "coordinates": [[p[1], p[0]] for p in path]}


@lru_cache(maxsize=4)
def _load(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def load_network(path: Path | None = None) -> dict[str, Any]:
    return _load(str(path or get_settings().DEMO_NETWORK_CONFIG))


def camera_edges(net: dict[str, Any], free_flow_kmh: float = 80.0, typical_kmh: float = 30.0) -> list[dict[str, Any]]:
    """Directed camera-graph edges derived from monitored roads (both ways unless one-way)."""
    edges = []
    for r in net["roads"]:
        if not r.get("from") or not r.get("to"):
            continue
        dist = path_length_m(r["path"])
        pairs = [(r["from"], r["to"], r["path"], True)]
        pairs.append((r["to"], r["from"], list(reversed(r["path"])), not r.get("one_way", False)))
        for a, b, p, allowed in pairs:
            edges.append({
                "from_camera_id": a, "to_camera_id": b, "distance_m": round(dist, 1),
                "min_travel_s": round(dist / (free_flow_kmh / 3.6), 1),
                "typical_travel_s": round(dist / (typical_kmh / 3.6), 1),
                "road_name": r["name"], "road_id": r["id"],
                "direction": _compass(bearing_deg(p[0][0], p[0][1], p[-1][0], p[-1][1])),
                "allowed": allowed, "path": p,
            })
    # merge duplicates (keep the allowed/shortest variant per ordered pair)
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for e in edges:
        k = (e["from_camera_id"], e["to_camera_id"])
        cur = best.get(k)
        if cur is None or (e["allowed"] and not cur["allowed"]) or (e["allowed"] == cur["allowed"] and e["distance_m"] < cur["distance_m"]):
            best[k] = e
    return list(best.values())


def _compass(deg: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((deg % 360) + 22.5) // 45) % 8]
