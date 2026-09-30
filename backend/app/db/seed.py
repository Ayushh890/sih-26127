"""Idempotent start-up seeding.

* roles and their permissions (always kept in sync with :mod:`app.auth.permissions`)
* bootstrap administrator from ``ADMIN_USERNAME``/``ADMIN_PASSWORD`` (if set)
* demo accounts (``DEMO_USERS``; refused in production) — one per role
* the synthetic demo network (``SEED_DEMO``): demo cameras, roads and camera-graph edges,
  all flagged ``is_demo`` so their data is never mixed with live cameras
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.permissions import ROLE_DESCRIPTIONS, ROLE_PERMISSIONS
from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import hash_password
from app.db.models import Camera, CameraEdge, Road, Role, User
from app.demo.network import camera_edges, load_network, path_to_geojson
from app.demo.renderer import SceneRenderer
from app.demo.scenario import get_scenario
from app.services.topology import invalidate_topology

log = get_logger("seed")

DEMO_ACCOUNTS = [("admin", "Demo Administrator", "admin"), ("operator", "Demo Traffic Operator", "operator"),
                 ("analyst", "Demo Traffic Analyst", "analyst"), ("viewer", "Demo Read-only User", "viewer")]
DEMO_PROCESSING = {"confidence_threshold": 0.3, "min_plate_vehicle_width": 140, "frame_skip": 0, "max_queue_size": 4}


def seed_roles(db: Session) -> dict[str, Role]:
    roles = {r.name: r for r in db.scalars(select(Role))}
    for name, perms in ROLE_PERMISSIONS.items():
        r = roles.get(name)
        if r is None:
            r = Role(name=name)
            db.add(r)
            roles[name] = r
        r.description = ROLE_DESCRIPTIONS[name]
        r.permissions = sorted(p.value for p in perms)
    db.flush()
    return roles


def seed_users(db: Session, roles: dict[str, Role]) -> list[str]:
    s = get_settings()
    created = []
    if s.ADMIN_USERNAME and s.ADMIN_PASSWORD and db.scalar(select(User).where(User.username == s.ADMIN_USERNAME)) is None:
        db.add(User(username=s.ADMIN_USERNAME, full_name="Administrator", password_hash=hash_password(s.ADMIN_PASSWORD), role_id=roles["admin"].id))
        created.append(s.ADMIN_USERNAME)
    if s.DEMO_USERS and s.APP_ENV != "production":
        for username, full, role in DEMO_ACCOUNTS:
            if db.scalar(select(User).where(User.username == username)) is None:
                db.add(User(username=username, full_name=full, password_hash=hash_password(s.DEMO_PASSWORD), role_id=roles[role].id, is_demo=True))
                created.append(username)
    db.flush()
    return created


def seed_demo_network(db: Session, enable: bool) -> dict[str, Any]:
    net = load_network()
    scenario = get_scenario()
    existing = set(db.scalars(select(Camera.id)))
    only = get_settings().demo_camera_ids
    added = []
    for c in net["cameras"]:
        if c["id"] in existing:
            continue
        cal = SceneRenderer(c["id"], scenario).calibration()
        source_type, uri, options = "demo", f"demo://{c['id']}", {}
        video = get_settings().demo_video_dir / f"{c['id']}.mp4"
        if get_settings().DEMO_SOURCE == "recorded":
            if video.exists():
                source_type, uri, options = "file", str(video), {"wall_clock_sync": True, "loop": True}
            else:
                log.warning("DEMO_SOURCE=recorded but %s is missing (run scripts/generate_demo_videos.py); %s renders live", video.name, c["id"])
        db.add(Camera(id=c["id"], name=c["name"], location=f"{c['road_name']}, {c['zone']} (synthetic)", latitude=c["lat"], longitude=c["lon"],
                      source_type=source_type, source_uri=uri, resolution="{}x{}".format(*net["frame_size"]), lane_count=c["lane_count"],
                      direction=c["direction"], road_name=c["road_name"], zone=c["zone"], camera_type=c.get("camera_type", "ANPR"),
                      enabled=enable and (not only or c["id"] in only),
                      status="OFFLINE", processing={"processing_fps": get_settings().demo_processing_fps, **DEMO_PROCESSING, "source_options": options}, calibration=cal, is_demo=True))
        added.append(c["id"])
    db.flush()
    roads: dict[str, Road] = {}
    if added:
        for r in net["roads"]:
            road = Road(name=r["name"], geojson=path_to_geojson(r["path"]), speed_limit_kmh=40.0, lanes=2, is_demo=True)
            db.add(road)
            roads[r["id"]] = road
        db.flush()
        have = {(e.from_camera_id, e.to_camera_id) for e in db.scalars(select(CameraEdge))}
        for e in camera_edges(net):
            if (e["from_camera_id"], e["to_camera_id"]) in have:
                continue
            db.add(CameraEdge(from_camera_id=e["from_camera_id"], to_camera_id=e["to_camera_id"], distance_m=e["distance_m"],
                              min_travel_s=e["min_travel_s"], typical_travel_s=e["typical_travel_s"], road_name=e["road_name"], direction=e["direction"],
                              allowed=e["allowed"], road_id=roads[e["road_id"]].id if e["road_id"] in roads else None,
                              path_geojson=path_to_geojson(e["path"])))
        invalidate_topology()
    return {"cameras_added": added, "roads_added": len(roads)}


def seed_all(db: Session) -> dict[str, Any]:
    s = get_settings()
    roles = seed_roles(db)
    users = seed_users(db, roles)
    demo = seed_demo_network(db, enable=s.DEMO_AUTOSTART) if s.SEED_DEMO else {"cameras_added": [], "roads_added": 0}
    db.commit()
    if users or demo["cameras_added"]:
        log.info("seeded users=%s demo=%s", users, demo)
    return {"users": users, **demo}
