"""Watchlist matching. Entries are created only by authorised users; nothing is pre-listed.

``exact`` entries match the normalised plate; ``fuzzy`` entries also match plates within
a confusion-aware edit distance (``fuzzy_max_distance``), which catches OCR confusions
such as 0/O or 8/B while reporting the distance in the alert.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import VehicleObservation, WatchlistEntry
from app.ml.ocr.normalize import weighted_edit_distance


def active_entries(db: Session, now: datetime | None = None) -> list[WatchlistEntry]:
    now = now or datetime.now(timezone.utc)
    return list(db.scalars(select(WatchlistEntry).where(WatchlistEntry.active.is_(True),
                                                         or_(WatchlistEntry.expires_at.is_(None), WatchlistEntry.expires_at > now))))


def match_plate(entries: list[WatchlistEntry], plate: str | None, fuzzy_max: float) -> list[tuple[WatchlistEntry, float]]:
    if not plate:
        return []
    out = []
    for e in entries:
        if e.plate == plate:
            out.append((e, 0.0))
        elif e.match_mode == "fuzzy" and abs(len(e.plate) - len(plate)) <= 1:
            d = weighted_edit_distance(e.plate, plate)
            if d <= fuzzy_max:
                out.append((e, round(d, 2)))
    return out


def retrospective(db: Session, entry: WatchlistEntry, days: int, fuzzy_max: float, limit: int = 200) -> list[tuple[VehicleObservation, float]]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    q = select(VehicleObservation).where(VehicleObservation.observed_at >= since, VehicleObservation.plate_text.is_not(None))
    if entry.match_mode == "exact":
        q = q.where(VehicleObservation.plate_text == entry.plate)
    rows = db.scalars(q.order_by(VehicleObservation.observed_at.desc()).limit(5000 if entry.match_mode == "fuzzy" else limit))
    out = []
    for o in rows:
        hits = match_plate([entry], o.plate_text, fuzzy_max)
        if hits:
            out.append((o, hits[0][1]))
        if len(out) >= limit:
            break
    return out


def entry_dict(e: WatchlistEntry) -> dict[str, Any]:
    return {"id": e.id, "plate": e.plate, "description": e.description, "reason": e.reason, "priority": e.priority, "match_mode": e.match_mode,
            "active": e.active, "expires_at": e.expires_at.isoformat() if e.expires_at else None, "created_by": e.created_by,
            "created_at": e.created_at.isoformat() if e.created_at else None, "last_match_at": e.last_match_at.isoformat() if e.last_match_at else None,
            "match_count": e.match_count}
