"""Application configuration loaded from environment variables / .env.

Only *static* deployment configuration lives here. Operator-tunable parameters
(identity weights, alert thresholds, retention) live in the ``system_settings``
table and are managed by :mod:`app.services.settings_service`.
"""
from __future__ import annotations

import base64
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- general -----------------------------------------------------------
    APP_NAME: str = "NIRNAY"
    APP_ENV: Literal["development", "production", "test"] = "development"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # --- storage -----------------------------------------------------------
    DATABASE_URL: str = ""  # empty -> SQLite at DATA_DIR/nirnay.db
    DB_POOL_SIZE: int = 10
    DB_ECHO: bool = False
    REDIS_URL: str = ""  # empty -> in-process event bus (dev mode)
    DATA_DIR: Path = REPO_ROOT / "data"
    MODELS_DIR: Path = REPO_ROOT / "models"
    # built console (frontend/dist). When set, the API also serves the console on the same origin
    # (single-container deployments such as Railway/Render); Compose uses nginx instead.
    FRONTEND_DIR: Path | None = None
    MODEL_CONFIG: Path = REPO_ROOT / "configs" / "models.yaml"
    DEMO_NETWORK_CONFIG: Path = REPO_ROOT / "configs" / "demo_network.json"

    # --- processes -----------------------------------------------------------
    # embedded: the API process also runs camera workers + ingestion (no Redis needed)
    # external: a separate `python -m app.workers.main` process does the processing
    # api-only: API process only (used by tests or when scaling workers separately)
    WORKER_MODE: Literal["embedded", "external", "api-only"] = "embedded"
    WORKER_ID: str = "worker-1"
    # "index/count": this worker runs only the cameras whose id hashes to `index` (several
    # `--role streams` workers each take a disjoint share). "0/1" = all cameras.
    WORKER_SHARD: str = Field(default="0/1", pattern=r"^\d{1,3}/\d{1,3}$")

    # --- security ------------------------------------------------------------
    JWT_SECRET: str = "change-me-dev-only-secret"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 480
    ENCRYPTION_KEY: str = ""  # Fernet key; derived from JWT_SECRET in development if empty
    PSEUDONYM_SECRET: str = ""  # HMAC key for plate pseudonyms; derived if empty
    CORS_ORIGINS: list[str] = Field(default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"])
    RATE_LIMIT_PER_MINUTE: int = 600
    LOGIN_RATE_LIMIT_PER_MINUTE: int = 10
    PRIVACY_MODE: bool = True  # pseudonymise plates for analyst/viewer roles
    EVIDENCE_ENCRYPTION: bool = False  # Fernet-encrypt evidence images at rest
    EVIDENCE_MAX_MB: int = 2048  # disk guard: stop writing new evidence above this size
    EVIDENCE_JPEG_QUALITY: int = 72

    # --- cameras / pipeline ----------------------------------------------------
    CAMERA_TIMEOUT: float = 8.0  # seconds without a frame -> OFFLINE
    CAMERA_MAX_BACKOFF: float = 60.0
    CAMERA_DEGRADED_FPS_RATIO: float = 0.5
    DEFAULT_PROCESSING_FPS: float = 5.0
    DEFAULT_CONFIDENCE_THRESHOLD: float = 0.35
    DEFAULT_MAX_QUEUE_SIZE: int = 4
    OCR_CONFIDENCE_THRESHOLD: float = 0.55
    REID_SIMILARITY_THRESHOLD: float = 0.80
    INFERENCE_DEVICE: Literal["auto", "cpu", "gpu"] = "auto"
    INFERENCE_THREADS: int = 0  # ONNX Runtime intra-op threads per model; 0 = derive from the CPU budget
    LIVE_FRAME_FPS: float = 6.0  # rate at which preview JPEGs are published
    LIVE_FRAME_WIDTH: int = 960

    # --- demo --------------------------------------------------------------
    SEED_DEMO: bool = True
    DEMO_AUTOSTART: bool = True
    # synthetic: frames rendered live; recorded: replay MP4s from scripts/generate_demo_videos.py
    # through the ordinary video-file source (falls back to synthetic if a file is missing)
    DEMO_SOURCE: Literal["synthetic", "recorded"] = "synthetic"
    DEMO_USERS: bool = True
    DEMO_PROCESSING_FPS: float = 0  # processing rate of seeded demo cameras; 0 = derive from the CPU budget
    # comma-separated demo camera ids started automatically (empty = all). The others are still
    # seeded and can be started from the console; used to fit small cloud instances (~130 MB/camera).
    DEMO_CAMERAS: str = ""
    DEMO_PASSWORD: str = "nirnay-demo"  # password of the seeded demo accounts (development only)
    ADMIN_USERNAME: str = ""  # bootstrap administrator created on first start when set
    ADMIN_PASSWORD: str = ""

    # --- retention ------------------------------------------------------------
    RETENTION_OBSERVATION_DAYS: int = 30
    RETENTION_EVIDENCE_DAYS: int = 14
    RETENTION_HEALTH_DAYS: int = 7

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        if isinstance(v, str) and not v.strip().startswith("["):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _psycopg_driver(cls, v: object) -> object:
        # hosted PostgreSQL (Railway, Render, Heroku-style) hands out postgres:// or postgresql:// URLs
        if isinstance(v, str):
            for prefix in ("postgres://", "postgresql://"):
                if v.startswith(prefix):
                    return "postgresql+psycopg://" + v[len(prefix):]
        return v

    @model_validator(mode="after")
    def _default_sqlite(self) -> "Settings":
        # keep the zero-config database next to the evidence, i.e. on the data volume
        if not self.DATABASE_URL:
            self.DATABASE_URL = f"sqlite:///{self.DATA_DIR / 'nirnay.db'}"
        return self

    # --- derived helpers -------------------------------------------------------
    @property
    def demo_camera_ids(self) -> set[str]:
        return {c.strip() for c in self.DEMO_CAMERAS.split(",") if c.strip()}

    @property
    def is_sqlite(self) -> bool:
        return self.DATABASE_URL.startswith("sqlite")

    @property
    def evidence_dir(self) -> Path:
        return self.DATA_DIR / "evidence"

    @property
    def demo_video_dir(self) -> Path:
        return self.DATA_DIR / "demo" / "videos"

    @property
    def inference_threads(self) -> int:
        return self.INFERENCE_THREADS or max(1, min(4, effective_cpu_count() // 2))

    @property
    def demo_processing_fps(self) -> float:
        # ~1.5 processed frames/s per available core across the six demo cameras, bounded to 3..10 fps
        return self.DEMO_PROCESSING_FPS or float(max(3, min(10, round(effective_cpu_count() * 1.5))))

    @property
    def fernet_key(self) -> bytes:
        if self.ENCRYPTION_KEY:
            return self.ENCRYPTION_KEY.encode()
        digest = hashlib.sha256(("nirnay-fernet:" + self.JWT_SECRET).encode()).digest()
        return base64.urlsafe_b64encode(digest)

    @property
    def pseudonym_key(self) -> bytes:
        return (self.PSEUDONYM_SECRET or ("nirnay-pseudonym:" + self.JWT_SECRET)).encode()

    def validate_production(self) -> list[str]:
        """Return a list of configuration problems that are unacceptable in production."""
        problems: list[str] = []
        if self.APP_ENV == "production":
            if self.JWT_SECRET.startswith("change-me") or len(self.JWT_SECRET) < 32:
                problems.append("JWT_SECRET must be a random string of at least 32 characters")
            if not self.ENCRYPTION_KEY:
                problems.append("ENCRYPTION_KEY must be set (Fernet key) in production")
            if self.DEMO_USERS:
                problems.append("DEMO_USERS must be false in production")
        return problems


def effective_cpu_count() -> int:
    """CPUs this process may actually use: affinity mask, capped by a cgroup CPU quota (containers)."""
    try:
        n = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        n = os.cpu_count() or 1
    for path, parse in (("/sys/fs/cgroup/cpu.max", lambda t: t.split()), ("/sys/fs/cgroup/cpu/cpu.cfs_quota_us", lambda t: [t.strip(), None])):
        try:
            with open(path) as f:
                quota, period = parse(f.read())
            if quota not in ("max", "-1"):
                if period is None:
                    with open("/sys/fs/cgroup/cpu/cpu.cfs_period_us") as f:
                        period = f.read().strip()
                n = min(n, max(1, int(int(quota) / int(period))))
            break
        except (OSError, ValueError):
            continue
    return max(1, n)


@lru_cache
def get_settings() -> Settings:
    return Settings()
