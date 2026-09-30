"""Camera road graph: the directed edges used for travel-time plausibility, impossible
match rejection, next-camera prediction and trajectory road geometry."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, require
from app.auth.permissions import Perm
from app.db.models import Camera, CameraEdge, Road
from app.db.session import get_db
from app.schemas.requests import EdgeCreate, EdgeUpdate
from app.services import audit
from app.services.topology import get_topology, invalidate_topology

router = APIRouter(prefix="/api/topology", tags=["topology"])

# defaults when the operator gives only a distance: fastest plausible ≈ 90 km/h, typical ≈ 30 km/h
FAST_MPS = 25.0
TYPICAL_MPS = 8.33


def edge_dict(e: CameraEdge) -> dict[str, Any]:
    return {"id": e.id, "from_camera_id": e.from_camera_id, "to_camera_id": e.to_camera_id, "distance_m": e.distance_m,
            "min_travel_s": e.min_travel_s, "typical_travel_s": e.typical_travel_s, "road_name": e.road_name, "direction": e.direction,
            "allowed": e.allowed, "road_id": e.road_id, "has_geometry": bool(e.path_geojson)}


@router.get("", summary="Topology graph: edges, roads and GeoJSON")
def get_graph(user: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    g = get_topology(db)
    roads = [{"id": r.id, "name": r.name, "geojson": r.geojson, "speed_limit_kmh": r.speed_limit_kmh, "lanes": r.lanes, "is_demo": r.is_demo}
             for r in db.scalars(select(Road).order_by(Road.id))]
    edges = [edge_dict(e) for e in db.scalars(select(CameraEdge).order_by(CameraEdge.from_camera_id, CameraEdge.to_camera_id))]
    return {"edges": edges, "roads": roads, "geojson": g.geojson(),
            "cameras": [{"id": k, **v} for k, v in sorted(g.cameras.items())]}


@router.get("/path", summary="Shortest allowed path between two cameras")
def path(from_camera_id: str = Query(alias="from", max_length=32), to_camera_id: str = Query(alias="to", max_length=32),
         user: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    g = get_topology(db)
    if from_camera_id not in g.cameras or to_camera_id not in g.cameras:
        raise not_found("camera")
    p = g.shortest(from_camera_id, to_camera_id)
    if p is None:
        return {"reachable": False, "from": from_camera_id, "to": to_camera_id,
                "explanation": "no allowed directed path: a match between these cameras in this order would be rejected"}
    return {"reachable": True, **p.as_dict(), "edges": [{"from": e.from_id, "to": e.to_id, "road_name": e.road_name,
                                                        "distance_m": e.distance_m, "min_travel_s": e.min_travel_s} for e in p.edges]}


def _make(db: Session, body: EdgeCreate, a: str, b: str) -> CameraEdge:
    if db.scalar(select(CameraEdge).where(CameraEdge.from_camera_id == a, CameraEdge.to_camera_id == b)):
        raise HTTPException(status.HTTP_409_CONFLICT, f"edge {a} → {b} already exists")
    pts = body.path if a == body.from_camera_id else (list(reversed(body.path)) if body.path else None)
    e = CameraEdge(from_camera_id=a, to_camera_id=b, distance_m=body.distance_m,
                   min_travel_s=body.min_travel_s or round(body.distance_m / FAST_MPS, 1),
                   typical_travel_s=body.typical_travel_s or round(body.distance_m / TYPICAL_MPS, 1), road_name=body.road_name,
                   direction=body.direction, allowed=body.allowed,
                   path_geojson={"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]} if pts else None)
    if e.min_travel_s > e.typical_travel_s:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "min_travel_s cannot exceed typical_travel_s")
    db.add(e)
    return e


@router.post("/edges", status_code=status.HTTP_201_CREATED, summary="Add a directed edge (optionally both directions)")
def create_edge(body: EdgeCreate, request: Request, user: CurrentUser = Depends(require(Perm.TOPOLOGY_WRITE)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    for cid in (body.from_camera_id, body.to_camera_id):
        if db.get(Camera, cid) is None:
            raise not_found(f"camera {cid}")
    if body.path and any(len(p) != 2 or not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180) for p in body.path):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "path must be a list of [lat, lon] pairs")
    made = [_make(db, body, body.from_camera_id, body.to_camera_id)]
    if body.bidirectional:
        made.append(_make(db, body, body.to_camera_id, body.from_camera_id))
    db.flush()
    audit.record(db, user.as_dict(), "topology.edge_create", "edge", ",".join(str(e.id) for e in made),
                 {"from": body.from_camera_id, "to": body.to_camera_id, "bidirectional": body.bidirectional}, client_ip(request))
    invalidate_topology()
    return {"edges": [edge_dict(e) for e in made]}


@router.patch("/edges/{edge_id}", summary="Update an edge (travel-time priors, allowed flag, direction, road geometry)")
def update_edge(edge_id: int, body: EdgeUpdate, request: Request, user: CurrentUser = Depends(require(Perm.TOPOLOGY_WRITE)),
                db: Session = Depends(get_db)) -> dict[str, Any]:
    e = db.get(CameraEdge, edge_id)
    if e is None:
        raise not_found("edge")
    changes = body.model_dump(exclude_unset=True)
    if "path" in changes:
        pts = changes.pop("path") or []
        if pts and (len(pts) < 2 or any(len(p) != 2 or not (-90 <= p[0] <= 90 and -180 <= p[1] <= 180) for p in pts)):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "path must be a list of at least two [lat, lon] pairs")
        e.path_geojson = {"type": "LineString", "coordinates": [[p[1], p[0]] for p in pts]} if pts else None
        changes["path_points"] = len(pts)
    for k, v in changes.items():
        if k != "path_points":
            setattr(e, k, v)
    if e.min_travel_s > e.typical_travel_s:
        db.rollback()
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "min_travel_s cannot exceed typical_travel_s")
    audit.record(db, user.as_dict(), "topology.edge_update", "edge", str(e.id), changes, client_ip(request))
    invalidate_topology()
    return edge_dict(e)


@router.delete("/edges/{edge_id}", summary="Delete an edge")
def delete_edge(edge_id: int, request: Request, user: CurrentUser = Depends(require(Perm.TOPOLOGY_WRITE)), db: Session = Depends(get_db)) -> dict[str, Any]:
    e = db.get(CameraEdge, edge_id)
    if e is None:
        raise not_found("edge")
    info = {"from": e.from_camera_id, "to": e.to_camera_id}
    db.delete(e)
    audit.record(db, user.as_dict(), "topology.edge_delete", "edge", str(edge_id), info, client_ip(request))
    invalidate_topology()
    return {"deleted": edge_id}


@router.get("/cameras/{camera_id}/neighbours", summary="Upstream/downstream cameras")
def neighbours(camera_id: str, user: CurrentUser = Depends(require(Perm.CAMERAS_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    g = get_topology(db)
    if camera_id not in g.cameras:
        raise not_found("camera")
    fmt = lambda e, other: {"camera_id": other, "distance_m": e.distance_m, "min_travel_s": e.min_travel_s,  # noqa: E731
                            "typical_travel_s": e.typical_travel_s, "road_name": e.road_name}
    return {"camera_id": camera_id, "upstream": [fmt(e, e.from_id) for e in g.incoming(camera_id)],
            "downstream": [fmt(e, e.to_id) for e in g.outgoing(camera_id)],
            "edges": [edge_dict(e) for e in db.scalars(select(CameraEdge).where(or_(CameraEdge.from_camera_id == camera_id,
                                                                                     CameraEdge.to_camera_id == camera_id)))]}
