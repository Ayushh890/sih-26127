"""Validated request bodies for the REST API.

All inputs are bounded (lengths, ranges, enumerations) and unknown fields are rejected,
so a malformed request fails with 422 before reaching any service code.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import unquote, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.camera.sources import FAULT_KINDS

COMPASS = Literal["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
SOURCE = Literal["rtsp", "http", "webcam", "browser", "file", "demo"]
PRIORITY = Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
ROLE = Literal["admin", "operator", "analyst", "viewer"]
_CAMERA_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$")
_RESOLUTION = re.compile(r"^\d{3,4}x\d{3,4}$")
_PLATE = re.compile(r"^[A-Z0-9]{4,12}$")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --------------------------------------------------------------------------- auth / users
class LoginRequest(Strict):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class UserCreate(Strict):
    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    full_name: str = Field(default="", max_length=128)
    password: str = Field(min_length=10, max_length=256)
    role: ROLE


class UserUpdate(Strict):
    full_name: str | None = Field(default=None, max_length=128)
    role: ROLE | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=10, max_length=256)


class PasswordChange(Strict):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=10, max_length=256)


# --------------------------------------------------------------------------- cameras
class SourceOptions(BaseModel):
    model_config = ConfigDict(extra="allow")
    loop: bool | None = None  # video files
    epoch_offset_s: float | None = None  # demo timeline
    open_timeout_s: float | None = Field(default=None, ge=1, le=60)
    transport: Literal["tcp", "udp"] | None = None  # RTSP transport


class ProcessingConfig(Strict):
    processing_fps: float = Field(default=5.0, ge=0.5, le=30)
    confidence_threshold: float = Field(default=0.35, ge=0.05, le=0.95)
    plate_confidence: float | None = Field(default=None, ge=0.05, le=0.95)
    ocr_confidence_threshold: float | None = Field(default=None, ge=0.05, le=0.99)
    min_plate_vehicle_width: int | None = Field(default=None, ge=40, le=1000)
    max_reads_per_track: int | None = Field(default=None, ge=1, le=50)
    queue_speed_kmh: float | None = Field(default=None, ge=1, le=30)
    evidence_full_frames: bool | None = None
    frame_skip: int = Field(default=0, ge=0, le=30)
    max_queue_size: int = Field(default=4, ge=1, le=64)
    bucket_seconds: int | None = Field(default=None, ge=15, le=900)
    source_options: SourceOptions | None = None


class CalibrationConfig(Strict):
    reference_width: int | None = Field(default=None, ge=160, le=7680)
    focal_px: float | None = Field(default=None, gt=0, le=20000)
    flow_toward_camera: bool | None = None
    one_way: bool | None = None
    vanishing_point: list[float] | None = Field(default=None, min_length=2, max_length=2)
    lane_boundaries: list[float] | None = Field(default=None, min_length=2, max_length=13)
    homography: list[list[float]] | None = None
    camera_height_m: float | None = Field(default=None, gt=0, le=50)
    speed_limit_kmh: float | None = Field(default=None, gt=0, le=200)
    source: str | None = Field(default=None, max_length=64)

    @field_validator("homography")
    @classmethod
    def _h(cls, v: list[list[float]] | None) -> list[list[float]] | None:
        if v is not None and (len(v) != 3 or any(len(r) != 3 for r in v)):
            raise ValueError("homography must be a 3x3 matrix")
        return v

    @field_validator("vanishing_point", "lane_boundaries")
    @classmethod
    def _fractions(cls, v: list[float] | None) -> list[float] | None:
        if v is not None and any(not (-0.5 <= x <= 1.5) for x in v):
            raise ValueError("values are fractions of the frame (0..1)")
        return v


def split_credentials(uri: str) -> tuple[str, str | None, str | None]:
    """Remove ``user:pass@`` from a URI so credentials are stored encrypted, never in the URI."""
    parts = urlsplit(uri)
    if not parts.username and not parts.password:
        return uri, None, None
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment)), \
        unquote(parts.username) if parts.username else None, unquote(parts.password) if parts.password else None


class _CameraFields(Strict):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    location: str | None = Field(default=None, max_length=255)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    source_type: SOURCE | None = None
    source_uri: str | None = Field(default=None, min_length=1, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)  # write-only: stored encrypted, never returned
    resolution: str | None = Field(default=None, max_length=16)
    lane_count: int | None = Field(default=None, ge=1, le=12)
    direction: COMPASS | None = None
    road_name: str | None = Field(default=None, max_length=128)
    zone: str | None = Field(default=None, max_length=64)
    camera_type: Literal["ANPR", "SURVEILLANCE", "PTZ", "OVERVIEW"] | None = None
    enabled: bool | None = None
    processing: ProcessingConfig | None = None
    calibration: CalibrationConfig | None = None

    @field_validator("resolution")
    @classmethod
    def _res(cls, v: str | None) -> str | None:
        if v and not _RESOLUTION.match(v):
            raise ValueError("resolution must look like 1280x720")
        return v or None

    @model_validator(mode="after")
    def _uri(self) -> "_CameraFields":
        if self.source_uri and self.source_type:
            u = self.source_uri
            ok = {"rtsp": ("rtsp://", "rtsps://"), "http": ("http://", "https://"), "browser": ("browser://",), "demo": ("demo://",)}.get(self.source_type)
            if ok and not u.lower().startswith(ok):
                raise ValueError(f"{self.source_type} source URI must start with {' or '.join(ok)}")
            if self.source_type == "webcam" and not re.fullmatch(r"\d{1,2}|/dev/video\d{1,2}", u):
                raise ValueError("webcam source must be a device index (0, 1, …) or /dev/videoN")
            if self.source_type == "file" and ("\x00" in u or u.startswith(("http:", "rtsp:"))):
                raise ValueError("file source must be a local file path")
        if self.source_uri and self.source_type in ("rtsp", "http"):
            clean, user, pw = split_credentials(self.source_uri)
            self.source_uri = clean
            if user and not self.username:
                self.username = user
            if pw and not self.password:
                self.password = pw
        return self


class CameraCreate(_CameraFields):
    id: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=1, max_length=128)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    source_type: SOURCE
    source_uri: str = Field(min_length=1, max_length=2048)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not _CAMERA_ID.match(v):
            raise ValueError("camera id may contain letters, digits, '-' and '_' (2–32 characters)")
        return v.upper()


class CameraUpdate(_CameraFields):
    clear_credentials: bool = False


class ConnectionTest(Strict):
    source_type: SOURCE
    source_uri: str = Field(min_length=1, max_length=2048)
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    camera_id: str | None = Field(default=None, max_length=32)  # reuse the stored password of this camera
    timeout_s: float = Field(default=8.0, ge=1, le=30)

    @model_validator(mode="after")
    def _split(self) -> "ConnectionTest":
        if self.source_type in ("rtsp", "http"):
            clean, user, pw = split_credentials(self.source_uri)
            self.source_uri, self.username, self.password = clean, self.username or user, self.password or pw
        return self


class FaultInjection(Strict):
    kind: str = Field(default="offline")
    seconds: float = Field(default=30, ge=1, le=600)

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        if v not in FAULT_KINDS:
            raise ValueError(f"kind must be one of {', '.join(FAULT_KINDS)}")
        return v


class OnvifDiscover(Strict):
    timeout_s: float = Field(default=3.0, ge=0.5, le=15)


class OnvifProbe(Strict):
    xaddr: str = Field(min_length=8, max_length=512, pattern=r"^https?://")
    username: str | None = Field(default=None, max_length=128)
    password: str | None = Field(default=None, max_length=256)
    timeout_s: float = Field(default=5.0, ge=1, le=20)


# --------------------------------------------------------------------------- topology
class EdgeCreate(Strict):
    from_camera_id: str = Field(max_length=32)
    to_camera_id: str = Field(max_length=32)
    distance_m: float = Field(gt=0, le=100000)
    min_travel_s: float | None = Field(default=None, gt=0, le=36000)
    typical_travel_s: float | None = Field(default=None, gt=0, le=36000)
    road_name: str = Field(default="", max_length=128)
    direction: str = Field(default="", max_length=8)
    allowed: bool = True
    path: list[list[float]] | None = Field(default=None, max_length=5000, description="[[lat, lon], ...]")
    bidirectional: bool = False

    @model_validator(mode="after")
    def _check(self) -> "EdgeCreate":
        if self.from_camera_id == self.to_camera_id:
            raise ValueError("an edge must connect two different cameras")
        if self.min_travel_s and self.typical_travel_s and self.min_travel_s > self.typical_travel_s:
            raise ValueError("min_travel_s cannot exceed typical_travel_s")
        return self


class EdgeUpdate(Strict):
    distance_m: float | None = Field(default=None, gt=0, le=100000)
    min_travel_s: float | None = Field(default=None, gt=0, le=36000)
    typical_travel_s: float | None = Field(default=None, gt=0, le=36000)
    road_name: str | None = Field(default=None, max_length=128)
    direction: str | None = Field(default=None, max_length=8)
    allowed: bool | None = None
    path: list[list[float]] | None = Field(default=None, max_length=5000, description="[[lat, lon], ...]; [] clears the geometry")


# --------------------------------------------------------------------------- alerts / watchlist
class AlertAction(Strict):
    note: str | None = Field(default=None, max_length=2000)


class WatchlistCreate(Strict):
    plate: str = Field(min_length=4, max_length=16)
    reason: str = Field(min_length=3, max_length=255)
    description: str = Field(default="", max_length=255)
    priority: PRIORITY = "MEDIUM"
    match_mode: Literal["exact", "fuzzy"] = "exact"
    expires_at: datetime | None = None

    @field_validator("plate")
    @classmethod
    def _plate(cls, v: str) -> str:
        v = re.sub(r"[\s\-.]", "", v.upper())
        if not _PLATE.match(v):
            raise ValueError("plate must be 4–12 letters/digits")
        return v


class WatchlistUpdate(Strict):
    reason: str | None = Field(default=None, min_length=3, max_length=255)
    description: str | None = Field(default=None, max_length=255)
    priority: PRIORITY | None = None
    match_mode: Literal["exact", "fuzzy"] | None = None
    active: bool | None = None
    expires_at: datetime | None = None


# --------------------------------------------------------------------------- corridor / settings
class CorridorRequest(Strict):
    origin: str = Field(max_length=32)
    destination: str = Field(max_length=32)
    depart: datetime | None = None
    priority_speedup: float = Field(default=0.35, ge=0, le=0.8)


class SettingsUpdate(Strict):
    value: dict[str, Any]


class IncidentQuery(Strict):
    camera_id: str = Field(max_length=32)
    start: datetime
    end: datetime | None = None
