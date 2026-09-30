"""Emergency corridor planner — SIMULATION ONLY.

Plans a route for an emergency vehicle over the camera graph using current observed
travel times, estimates the ETA at every junction and produces advisory actions for
traffic personnel. It never sends commands to traffic signals or any field device.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.services import analytics
from app.services.topology import PathInfo, TopologyGraph
from app.services.travel_times import baselines

DISCLAIMER = ("Simulation only: advisory output for traffic personnel. NIRNAY does not control traffic signals or any field equipment.")


def _route_time(path: PathInfo, tt: dict[tuple[str, str], float]) -> float:
    return sum(tt.get((e.from_id, e.to_id), e.typical_travel_s) for e in path.edges)


def plan(db: Session, graph: TopologyGraph, congestion_cfg: dict[str, Any], origin: str, destination: str, depart: datetime | None = None,
         priority_speedup: float = 0.35) -> dict[str, Any]:
    if origin not in graph.cameras or destination not in graph.cameras:
        raise ValueError("unknown origin or destination camera")
    if origin == destination:
        raise ValueError("origin and destination must differ")
    depart = depart or datetime.now(timezone.utc)
    base = baselines(db, graph, congestion_cfg["baseline_s"], 0)
    tt = {k: v["seconds"] for k, v in base.items()}
    cams = list(graph.cameras)
    cong = {c["camera_id"]: c for c in analytics.network_congestion(db, congestion_cfg, graph, cams)}
    # congestion-aware cost: travel time inflated by the destination junction's congestion score
    cost = {k: s * (1 + (cong.get(k[1], {}).get("score") or 0) / 100.0) for k, s in tt.items()}
    primary = graph.shortest(origin, destination, weight="typical_travel_s", travel_times=cost)
    if primary is None:
        raise ValueError(f"no allowed road path from {origin} to {destination}")
    alternatives = []
    for mid in primary.cameras[1:-1]:  # alternative avoiding each intermediate junction
        penalised = {**cost, **{(e.from_id, e.to_id): 1e9 for e in graph.incoming(mid)}}
        alt = graph.shortest(origin, destination, weight="typical_travel_s", travel_times=penalised)
        if alt and alt.cameras != primary.cameras and all(cost.get((e.from_id, e.to_id), 0) < 1e9 for e in alt.edges):
            if alt.cameras not in [a["cameras"] for a in alternatives]:
                alternatives.append({"cameras": alt.cameras, "distance_m": round(alt.distance_m, 1), "estimated_s": round(_route_time(alt, tt), 1),
                                     "avoids": mid})

    steps = []
    t = 0.0
    t_priority = 0.0
    for i, cam_id in enumerate(primary.cameras):
        c = cong.get(cam_id, {})
        if i > 0:
            e = primary.edges[i - 1]
            seg = tt.get((e.from_id, e.to_id), e.typical_travel_s)
            t += seg
            t_priority += max(e.min_travel_s, seg * (1 - priority_speedup))
        level = c.get("level", "NO_DATA")
        if i == 0:
            action = "Dispatch point."
        elif level in ("HEAVY", "SEVERE"):
            action = (f"Advise personnel at {cam_id} to clear the queue ({c.get('metrics', {}).get('queue', 0) or 0:.0f} queued vehicles) "
                      f"and hold cross traffic from ETA−60 s.")
        elif level == "MODERATE":
            action = f"Advise personnel at {cam_id} to prepare a green wave from ETA−30 s."
        else:
            action = f"Junction {cam_id} flowing freely; monitor only."
        steps.append({"camera_id": cam_id, "camera_name": graph.cameras[cam_id]["name"], "latitude": graph.cameras[cam_id]["lat"],
                      "longitude": graph.cameras[cam_id]["lon"], "eta_s": round(t, 1), "eta": (depart + timedelta(seconds=t)).isoformat(),
                      "eta_with_priority_s": round(t_priority, 1), "congestion_level": level, "congestion_score": c.get("score"),
                      "advisory": action})
    geo = []
    for e in primary.edges:
        geo.extend([[p[1], p[0]] for p in (e.path or [])] or [[graph.cameras[e.from_id]["lon"], graph.cameras[e.from_id]["lat"]],
                                                              [graph.cameras[e.to_id]["lon"], graph.cameras[e.to_id]["lat"]]])
    return {
        "simulation": True, "disclaimer": DISCLAIMER, "origin": origin, "destination": destination, "depart": depart.isoformat(),
        "route": primary.as_dict(), "estimated_s": round(t, 1), "estimated_with_priority_s": round(t_priority, 1),
        "time_saved_s": round(t - t_priority, 1), "steps": steps, "alternatives": alternatives,
        "geometry": {"type": "LineString", "coordinates": geo},
        "assumptions": [f"segment times = observed medians where ≥5 linked vehicles exist, otherwise topology prior",
                        f"priority clearance assumed to cut segment time by {priority_speedup:.0%}, never below the physical minimum",
                        "routing cost inflates travel time by each junction's current congestion score"],
    }
