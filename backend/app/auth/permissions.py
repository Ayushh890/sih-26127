"""Role-based access control: roles and the permissions they grant."""
from __future__ import annotations

from enum import StrEnum


class Perm(StrEnum):
    CAMERAS_READ = "cameras:read"
    CAMERAS_WRITE = "cameras:write"
    CAMERAS_CONTROL = "cameras:control"
    STREAM_VIEW = "stream:view"
    TOPOLOGY_WRITE = "topology:write"
    VEHICLES_SEARCH = "vehicles:search"
    PLATES_VIEW_RAW = "plates:view_raw"  # see un-pseudonymised plate text
    TRAJECTORY_READ = "trajectory:read"
    EVIDENCE_READ = "evidence:read"
    ANALYTICS_READ = "analytics:read"
    OD_INDIVIDUAL = "analytics:od_individual"  # drill into individual journeys
    ALERTS_READ = "alerts:read"
    ALERTS_ACT = "alerts:act"
    WATCHLIST_READ = "watchlist:read"
    WATCHLIST_WRITE = "watchlist:write"
    CORRIDOR_USE = "corridor:use"
    SYSTEM_READ = "system:read"
    SETTINGS_WRITE = "settings:write"
    AUDIT_READ = "audit:read"
    USERS_MANAGE = "users:manage"


ROLE_DESCRIPTIONS: dict[str, str] = {
    "admin": "Full administrative access including users, settings and audit logs",
    "operator": "Traffic operator: cameras, live monitoring, search, evidence, alerts, watchlist",
    "analyst": "Traffic analyst: aggregate analytics; vehicle data is pseudonymised",
    "viewer": "Read-only access to dashboards, camera status and aggregate analytics",
}

ROLE_PERMISSIONS: dict[str, set[Perm]] = {
    "admin": set(Perm),
    "operator": {
        Perm.CAMERAS_READ, Perm.CAMERAS_WRITE, Perm.CAMERAS_CONTROL, Perm.STREAM_VIEW,
        Perm.VEHICLES_SEARCH, Perm.PLATES_VIEW_RAW, Perm.TRAJECTORY_READ, Perm.EVIDENCE_READ,
        Perm.ANALYTICS_READ, Perm.OD_INDIVIDUAL, Perm.ALERTS_READ, Perm.ALERTS_ACT,
        Perm.WATCHLIST_READ, Perm.WATCHLIST_WRITE, Perm.CORRIDOR_USE, Perm.SYSTEM_READ,
    },
    "analyst": {
        Perm.CAMERAS_READ, Perm.STREAM_VIEW, Perm.VEHICLES_SEARCH, Perm.TRAJECTORY_READ,
        Perm.ANALYTICS_READ, Perm.ALERTS_READ, Perm.SYSTEM_READ, Perm.CORRIDOR_USE,
    },
    "viewer": {Perm.CAMERAS_READ, Perm.ANALYTICS_READ, Perm.ALERTS_READ, Perm.SYSTEM_READ},
}


def permissions_for(role: str) -> set[Perm]:
    return ROLE_PERMISSIONS.get(role, set())


def has_perm(role: str, perm: Perm) -> bool:
    return perm in permissions_for(role)
