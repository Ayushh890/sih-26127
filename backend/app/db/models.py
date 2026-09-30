"""ORM models.

Geospatial note: every spatial entity stores plain ``latitude``/``longitude`` (or
GeoJSON) columns so the schema works on SQLite for development. On PostgreSQL
the Alembic migration additionally creates PostGIS ``geom`` columns that are
*generated* from those values, with GiST indexes, so spatial SQL is available
without the ORM having to manage geometry objects.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, JSONType, UTCDateTime, utcnow


# --------------------------------------------------------------------------- identity & access
class Role(Base):
    __tablename__ = "roles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    permissions: Mapped[list] = mapped_column(JSONType, default=list)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role_id: Mapped[int] = mapped_column(ForeignKey("roles.id"))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    role: Mapped[Role] = relationship(lazy="joined")


# --------------------------------------------------------------------------- cameras & topology
class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # operator-assigned, e.g. CAM-01
    name: Mapped[str] = mapped_column(String(128))
    location: Mapped[str] = mapped_column(String(255), default="")
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(16))  # rtsp|http|webcam|file|demo
    source_uri: Mapped[str] = mapped_column(Text)  # credentials are never stored here
    username: Mapped[str | None] = mapped_column(String(128), nullable=True)
    password_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)  # e.g. 1280x720
    lane_count: Mapped[int] = mapped_column(Integer, default=2)
    direction: Mapped[str] = mapped_column(String(8), default="N")  # compass direction of monitored flow
    road_name: Mapped[str] = mapped_column(String(128), default="")
    zone: Mapped[str] = mapped_column(String(64), default="")
    camera_type: Mapped[str] = mapped_column(String(16), default="ANPR")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # desired state: processing on/off
    status: Mapped[str] = mapped_column(String(16), default="OFFLINE")
    status_message: Mapped[str] = mapped_column(Text, default="")
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    processing: Mapped[dict] = mapped_column(JSONType, default=dict)  # processing_fps, thresholds, ...
    calibration: Mapped[dict] = mapped_column(JSONType, default=dict)  # meters_per_pixel, speed_limit_kmh
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class CameraHealth(Base):
    __tablename__ = "camera_health"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    status: Mapped[str] = mapped_column(String(16))
    input_fps: Mapped[float] = mapped_column(Float, default=0.0)
    processing_fps: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    inference_ms: Mapped[float] = mapped_column(Float, default=0.0)
    queue_depth: Mapped[int] = mapped_column(Integer, default=0)
    frames_processed: Mapped[int] = mapped_column(Integer, default=0)
    frames_dropped: Mapped[int] = mapped_column(Integer, default=0)
    vehicles_detected: Mapped[int] = mapped_column(Integer, default=0)
    plates_read: Mapped[int] = mapped_column(Integer, default=0)
    avg_ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    blur_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    brightness: Mapped[float | None] = mapped_column(Float, nullable=True)
    reconnects: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    __table_args__ = (Index("ix_camera_health_cam_ts", "camera_id", "ts"),)


class Road(Base):
    __tablename__ = "roads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    geojson: Mapped[dict] = mapped_column(JSONType)  # GeoJSON LineString geometry
    speed_limit_kmh: Mapped[float] = mapped_column(Float, default=40.0)
    lanes: Mapped[int] = mapped_column(Integer, default=2)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class CameraEdge(Base):
    __tablename__ = "camera_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    to_camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    distance_m: Mapped[float] = mapped_column(Float)
    min_travel_s: Mapped[float] = mapped_column(Float)  # physically plausible minimum
    typical_travel_s: Mapped[float] = mapped_column(Float)  # free-flow expectation (baseline prior)
    road_name: Mapped[str] = mapped_column(String(128), default="")
    direction: Mapped[str] = mapped_column(String(8), default="")
    allowed: Mapped[bool] = mapped_column(Boolean, default=True)
    road_id: Mapped[int | None] = mapped_column(ForeignKey("roads.id", ondelete="SET NULL"), nullable=True)
    path_geojson: Mapped[dict | None] = mapped_column(JSONType, nullable=True)  # routed polyline
    __table_args__ = (UniqueConstraint("from_camera_id", "to_camera_id", name="uq_edge_pair"),)


# --------------------------------------------------------------------------- vehicles
class GlobalVehicle(Base):
    __tablename__ = "global_vehicles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # VEH-000012
    plate_text: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    plate_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    plate_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    vehicle_class: Mapped[str | None] = mapped_column(String(16), nullable=True)
    vehicle_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    first_camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    last_observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observation_count: Mapped[int] = mapped_column(Integer, default=0)
    camera_count: Mapped[int] = mapped_column(Integer, default=0)
    total_distance_m: Mapped[float] = mapped_column(Float, default=0.0)
    predicted_camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    predicted_from_observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    predicted_window_start: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    predicted_window_end: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    predicted_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class VehicleTrack(Base):
    """A single-camera track (one vehicle's passage through one camera's field of view)."""

    __tablename__ = "vehicle_tracks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    local_track_id: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime)
    ended_at: Mapped[datetime] = mapped_column(UTCDateTime)
    vehicle_class: Mapped[str] = mapped_column(String(16))
    class_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    frames: Mapped[int] = mapped_column(Integer, default=0)
    path: Mapped[list] = mapped_column(JSONType, default=list)  # sampled [t, cx, cy]
    __table_args__ = (Index("ix_vehicle_tracks_cam_started", "camera_id", "started_at"),)


class VehicleObservation(Base):
    __tablename__ = "vehicle_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)  # idempotency key from the worker
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    track_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_tracks.id", ondelete="SET NULL"), nullable=True)
    local_track_id: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime)
    plate_text: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)  # fused, normalised
    plate_raw: Mapped[str | None] = mapped_column(String(32), nullable=True)
    plate_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    plate_valid: Mapped[bool] = mapped_column(Boolean, default=False)
    plate_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    plate_votes: Mapped[int] = mapped_column(Integer, default=0)
    vehicle_class: Mapped[str] = mapped_column(String(16))
    class_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    vehicle_color: Mapped[str | None] = mapped_column(String(16), nullable=True)
    color_rgb: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    bbox: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    direction: Mapped[str | None] = mapped_column(String(8), nullable=True)  # compass
    heading_deg: Mapped[float | None] = mapped_column(Float, nullable=True)
    motion: Mapped[str | None] = mapped_column(String(16), nullable=True)  # with_flow|against_flow|stationary
    lane: Mapped[int | None] = mapped_column(Integer, nullable=True)
    global_vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("global_vehicles.id", ondelete="SET NULL"), nullable=True, index=True)
    previous_observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    match_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    match_confidence_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    match_reasons: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    match_components: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    global_vehicle: Mapped[GlobalVehicle | None] = relationship(lazy="select")
    __table_args__ = (
        Index("ix_obs_cam_time", "camera_id", "observed_at"),
        Index("ix_obs_gv_time", "global_vehicle_id", "observed_at"),
    )


class PlateRead(Base):
    __tablename__ = "plate_reads"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_id: Mapped[int | None] = mapped_column(ForeignKey("vehicle_observations.id", ondelete="CASCADE"), nullable=True, index=True)
    camera_id: Mapped[str] = mapped_column(String(32), index=True)
    local_track_id: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    raw_text: Mapped[str] = mapped_column(String(32))
    normalized_text: Mapped[str] = mapped_column(String(16), index=True)
    confidence: Mapped[float] = mapped_column(Float)
    char_confidences: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    is_valid_format: Mapped[bool] = mapped_column(Boolean, default=False)
    corrections: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    plate_bbox: Mapped[list | None] = mapped_column(JSONType, nullable=True)


class VehicleEmbedding(Base):
    __tablename__ = "vehicle_embeddings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("vehicle_observations.id", ondelete="CASCADE"), unique=True)
    model: Mapped[str] = mapped_column(String(64))
    dim: Mapped[int] = mapped_column(Integer)
    vector: Mapped[bytes] = mapped_column(LargeBinary)  # float32, L2-normalised
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class TrajectoryPoint(Base):
    __tablename__ = "trajectory_points"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    global_vehicle_id: Mapped[int] = mapped_column(ForeignKey("global_vehicles.id", ondelete="CASCADE"), index=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("vehicle_observations.id", ondelete="CASCADE"), unique=True)
    camera_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    seq: Mapped[int] = mapped_column(Integer)
    journey_index: Mapped[int] = mapped_column(Integer, default=0)
    from_camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    segment_travel_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    segment_distance_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    segment_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    link_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence_level: Mapped[str | None] = mapped_column(String(8), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    prediction_status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # CONFIRMED|MISSED
    __table_args__ = (Index("ix_traj_pair_time", "from_camera_id", "camera_id", "ts"),)


# --------------------------------------------------------------------------- traffic metrics
class TrafficMetric(Base):
    """Per-camera, per-minute frame-level statistics produced by the stream worker."""

    __tablename__ = "traffic_metrics"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    camera_id: Mapped[str] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"))
    bucket_start: Mapped[datetime] = mapped_column(UTCDateTime)
    bucket_seconds: Mapped[int] = mapped_column(Integer, default=60)
    frames: Mapped[int] = mapped_column(Integer, default=0)
    new_tracks: Mapped[int] = mapped_column(Integer, default=0)
    avg_vehicles_in_frame: Mapped[float] = mapped_column(Float, default=0.0)
    max_vehicles_in_frame: Mapped[int] = mapped_column(Integer, default=0)
    occupancy: Mapped[float] = mapped_column(Float, default=0.0)  # fraction of ROI covered by vehicles
    avg_stationary: Mapped[float] = mapped_column(Float, default=0.0)  # queue estimate (vehicles)
    avg_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("camera_id", "bucket_start", name="uq_metric_bucket"),)


# --------------------------------------------------------------------------- alerts, watchlist, evidence
class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)  # ALR-20260930-000123
    type: Mapped[str] = mapped_column(String(32), index=True)
    severity: Mapped[str] = mapped_column(String(10))  # INFO|LOW|MEDIUM|HIGH|CRITICAL
    status: Mapped[str] = mapped_column(String(14), default="NEW", index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    global_vehicle_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    title: Mapped[str] = mapped_column(String(255))
    reason: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)  # explanation bullets + metrics
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    evidence_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    dedup_key: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    occurrences: Mapped[int] = mapped_column(Integer, default=1)
    acknowledged_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=False)


class WatchlistEntry(Base):
    __tablename__ = "watchlist"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    plate: Mapped[str] = mapped_column(String(16), index=True)  # normalised
    description: Mapped[str] = mapped_column(String(255), default="")
    reason: Mapped[str] = mapped_column(String(255))
    priority: Mapped[str] = mapped_column(String(10), default="MEDIUM")  # LOW|MEDIUM|HIGH|CRITICAL
    match_mode: Mapped[str] = mapped_column(String(8), default="exact")  # exact|fuzzy
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    last_match_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    match_count: Mapped[int] = mapped_column(Integer, default=0)


class Evidence(Base):
    __tablename__ = "evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)  # uuid
    observation_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    camera_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    files: Mapped[dict] = mapped_column(JSONType, default=dict)  # role -> {path, sha256, width, height}
    bbox: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    plate_bbox: Mapped[list | None] = mapped_column(JSONType, nullable=True)
    ocr_text: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ocr_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    manifest_sha256: Mapped[str] = mapped_column(String(64))
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(64), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)


class SystemEvent(Base):
    """Operational events: camera errors, reconnects, dead-lettered ingest events, job failures."""

    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(10))
    source: Mapped[str] = mapped_column(String(32))
    camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    event_type: Mapped[str] = mapped_column(String(48))
    message: Mapped[str] = mapped_column(Text)
    details: Mapped[dict] = mapped_column(JSONType, default=dict)


class SystemSetting(Base):
    __tablename__ = "system_settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSONType)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"
    worker_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    info: Mapped[dict] = mapped_column(JSONType, default=dict)


class DeadLetter(Base):
    """Ingest events that could not be processed after retries (kept for inspection and replay)."""

    __tablename__ = "dead_letters"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(64))
    event_type: Mapped[str] = mapped_column(String(32), index=True)
    camera_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    payload: Mapped[dict] = mapped_column(JSONType)
    error: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(255), nullable=True)
