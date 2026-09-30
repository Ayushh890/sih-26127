"""Operator-tunable runtime settings stored in ``system_settings``.

Every section has code defaults; stored values are deep-merged over them so new
keys added in later versions appear automatically. Values are cached for a few
seconds so hot paths (identity matching, alert rules) don't hit the DB per event.
"""
from __future__ import annotations

import copy
import re
import threading
import time
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.models import SystemSetting


def _defaults() -> dict[str, dict[str, Any]]:
    s = get_settings()
    return {
        "identity": {
            # weights of the global identity score; renormalised over the signals available
            "weights": {"plate": 0.35, "appearance": 0.25, "time": 0.15, "route": 0.15, "attributes": 0.10},
            "accept_threshold": 0.62,  # minimum fused score to link two observations
            "high_confidence": 0.80,
            "medium_confidence": 0.65,
            "reid_similarity_threshold": s.REID_SIMILARITY_THRESHOLD,
            "plate_strong_similarity": 0.9,  # plate similarity treated as strong evidence
            "plate_min_confidence": 0.6,  # OCR confidence below which plates are "weak"
            "min_signal_for_link": 0.75,  # a link needs plate OR appearance at least this similar
            "max_link_window_s": 3600,
            "journey_gap_s": 1800,
            "time_tolerance": 0.15,  # allowed fraction below the edge's minimum travel time
            "max_hops": 3,
            "candidate_limit": 200,
            "ambiguity_margin": 0.05,  # appearance-only links need this lead over the runner-up
            "appearance_floor": 0.75,  # Re-ID cosine mapped to 0 at/below this value ...
            "appearance_ceiling": 0.97,  # ... and to 1 at/above this value
            "unconfirmed_plate_max_level": "MEDIUM",  # highest confidence level of a link without a confident plate match
        },
        "ocr": {
            "confidence_threshold": s.OCR_CONFIDENCE_THRESHOLD,
            "min_votes": 2,
            "max_reads_per_track": 12,
        },
        "congestion": {
            "window_s": 300,
            "baseline_s": 6 * 3600,
            "thresholds": {"moderate": 30, "heavy": 55, "severe": 75},
            "weights": {"speed": 0.35, "occupancy": 0.25, "queue": 0.2, "volume": 0.1, "travel_time": 0.1},
            "free_flow_speed_kmh": 40.0,
            "queue_saturation": 6.0,  # queued (slow/stopped) vehicles in view considered a full queue
            "occupancy_saturation": 0.35,  # share of road area covered by vehicles considered saturated
        },
        "alerts": {
            "WATCHLIST_MATCH": {"enabled": True, "fuzzy_max_distance": 1.0, "cooldown_s": 120},
            "REPEATED_SIGHTING": {"enabled": True, "severity": "LOW", "min_sightings": 3, "window_s": 1800, "cooldown_s": 1800},
            "IMPOSSIBLE_TRAVEL": {"enabled": True, "severity": "HIGH", "min_plate_confidence": 0.75, "max_ratio": 0.6, "cooldown_s": 600},
            "WRONG_WAY": {"enabled": True, "severity": "HIGH", "min_track_frames": 4, "cooldown_s": 60},
            "SEVERE_CONGESTION": {"enabled": True, "severity": "MEDIUM", "min_score": 75, "cooldown_s": 900},
            "CAMERA_OFFLINE": {"enabled": True, "severity": "HIGH", "cooldown_s": 300},
            "CAMERA_DEGRADED": {"enabled": True, "severity": "LOW", "min_duration_s": 30, "cooldown_s": 900},
            "OCR_DEGRADATION": {"enabled": True, "severity": "MEDIUM", "drop_ratio": 0.2, "min_reads": 8, "recent_s": 600, "window_s": 3600, "cooldown_s": 3600},
            "TRAFFIC_SURGE": {"enabled": True, "severity": "MEDIUM", "ratio": 1.8, "min_count": 8, "window_s": 300, "cooldown_s": 900},
            "TRAVEL_TIME_ANOMALY": {"enabled": True, "severity": "MEDIUM", "ratio": 1.5, "min_samples": 2, "window_s": 900, "cooldown_s": 900},
        },
        "retention": {
            "observation_days": s.RETENTION_OBSERVATION_DAYS,
            "evidence_days": s.RETENTION_EVIDENCE_DAYS,
            "health_days": s.RETENTION_HEALTH_DAYS,
        },
        "privacy": {
            "pseudonymise_for_roles": ["analyst", "viewer"] if s.PRIVACY_MODE else [],
            "od_min_count": 1,  # OD cells below this are suppressed (k-anonymity style)
        },
    }


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


SECTIONS = tuple(_defaults().keys())


class SettingsCache:
    def __init__(self, ttl: float = 5.0) -> None:
        self.ttl = ttl
        self._lock = threading.Lock()
        self._cache: dict[str, dict[str, Any]] = {}
        self._loaded = 0.0

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = 0.0

    def all(self, db: Session) -> dict[str, dict[str, Any]]:
        with self._lock:
            if time.time() - self._loaded < self.ttl and self._cache:
                return self._cache
        stored = {row.key: row.value for row in db.query(SystemSetting).all()}
        merged = {sec: _deep_merge(dflt, stored.get(sec) or {}) for sec, dflt in _defaults().items()}
        with self._lock:
            self._cache = merged
            self._loaded = time.time()
        return merged

    def section(self, db: Session, name: str) -> dict[str, Any]:
        return self.all(db)[name]


settings_cache = SettingsCache()


def update_section(db: Session, name: str, value: dict[str, Any], username: str) -> dict[str, Any]:
    if name not in SECTIONS:
        raise KeyError(name)
    row = db.get(SystemSetting, name)
    current = row.value if row else {}
    merged = _deep_merge(current, value)
    if row:
        row.value = merged
        row.updated_by = username
    else:
        db.add(SystemSetting(key=name, value=merged, updated_by=username))
    db.commit()
    settings_cache.invalidate()
    return settings_cache.section(db, name)


ENUM_SETTINGS: dict[tuple[str, str], set[str]] = {
    ("identity", "unconfirmed_plate_max_level"): {"HIGH", "MEDIUM", "LOW"},
}
SEVERITIES = {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
# settings that are fractions / probabilities
_UNIT_INTERVAL = re.compile(r"^(identity\.(weights\..+|accept_threshold|high_confidence|medium_confidence|reid_similarity_threshold|plate_strong_similarity"
                            r"|plate_min_confidence|min_signal_for_link|time_tolerance|ambiguity_margin|appearance_floor|appearance_ceiling)"
                            r"|ocr\.confidence_threshold|congestion\.weights\..+|congestion\.occupancy_saturation"
                            r"|alerts\.[A-Z_]+\.(min_plate_confidence|max_ratio|drop_ratio))$")
_PERCENT = re.compile(r"^(congestion\.thresholds\..+|alerts\.SEVERE_CONGESTION\.min_score)$")


def check_consistency(name: str, merged: dict[str, Any]) -> None:
    """Cross-field rules evaluated on the merged section (after a patch is applied)."""
    if name == "identity":
        if not merged["medium_confidence"] <= merged["high_confidence"]:
            raise ValueError("identity: medium_confidence must not exceed high_confidence")
        if not merged["appearance_floor"] < merged["appearance_ceiling"]:
            raise ValueError("identity: appearance_floor must be below appearance_ceiling")
        if sum(merged["weights"].values()) <= 0:
            raise ValueError("identity.weights: at least one weight must be positive")
    if name == "congestion":
        t = merged["thresholds"]
        if not t["moderate"] < t["heavy"] < t["severe"]:
            raise ValueError("congestion.thresholds: expected moderate < heavy < severe")
        if sum(merged["weights"].values()) <= 0:
            raise ValueError("congestion.weights: at least one weight must be positive")


def validate_patch(name: str, patch: dict[str, Any]) -> None:
    """Reject unknown keys and values whose type differs from the default (input validation)."""
    if name not in SECTIONS:
        raise KeyError(name)

    def walk(base: Any, value: Any, path: str) -> None:
        if isinstance(base, dict):
            if not isinstance(value, dict):
                raise ValueError(f"{path or name}: expected an object")
            for k, v in value.items():
                if k not in base:
                    raise ValueError(f"{path + '.' if path else ''}{k}: unknown setting")
                walk(base[k], v, f"{path + '.' if path else ''}{k}")
        elif isinstance(base, bool):
            if not isinstance(value, bool):
                raise ValueError(f"{path}: expected true/false")
        elif isinstance(base, (int, float)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{path}: expected a number")
            if value < 0:
                raise ValueError(f"{path}: must not be negative")
            full = f"{name}.{path}"
            if _UNIT_INTERVAL.match(full) and value > 1:
                raise ValueError(f"{path}: must be between 0 and 1")
            if _PERCENT.match(full) and value > 100:
                raise ValueError(f"{path}: must be between 0 and 100")
        elif isinstance(base, list):
            if not isinstance(value, list) or not all(isinstance(x, (str, int, float)) for x in value):
                raise ValueError(f"{path}: expected a list of scalars")
        elif isinstance(base, str):
            if not isinstance(value, str) or len(value) > 256:
                raise ValueError(f"{path}: expected a string (≤256 chars)")
            allowed = ENUM_SETTINGS.get((name, path)) or (SEVERITIES if name == "alerts" and path.endswith(".severity") else None)
            if allowed is not None and value not in allowed:
                raise ValueError(f"{path}: must be one of {', '.join(sorted(allowed))}")

    walk(_defaults()[name], patch, "")


def reset_section(db: Session, name: str) -> dict[str, Any]:
    row = db.get(SystemSetting, name)
    if row:
        db.delete(row)
        db.commit()
    settings_cache.invalidate()
    return settings_cache.section(db, name)
