"""Global vehicle identity: links a new camera observation to an existing vehicle.

For every candidate vehicle the new observation is compared with that vehicle's most
recent observation and a weighted fusion score is computed from the signals that are
actually available (weights from the ``identity`` settings section, renormalised):

* plate       – confusion-aware plate similarity, weight scaled by OCR reliability
* appearance  – cosine similarity of Re-ID embeddings (calibrated to [0, 1])
* time        – how plausible the elapsed time is against the baseline travel time
* route       – topology plausibility (direct neighbour, skipped cameras, loop)
* attributes  – vehicle class and colour agreement

Hard constraints reject a candidate outright: faster than physically possible over the
shortest allowed road path, no allowed path at all, or two plate reads that disagree
(both confident, or near-confident and sharing hardly any characters). Every decision carries a score, a confidence level and human-readable reasons;
rejected same-plate candidates are reported as conflicts (possible cloned plate).
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import math

import numpy as np
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GlobalVehicle, VehicleEmbedding, VehicleObservation
from app.ml.ocr.normalize import plate_similarity, weighted_edit_distance
from app.services.topology import PathInfo, TopologyGraph
from app.services.travel_times import path_baseline_s

SIMILAR_COLOURS = [{"white", "silver"}, {"silver", "grey"}, {"grey", "black"}, {"red", "orange"}, {"orange", "brown"}, {"orange", "yellow"}]
VEHICLE_FAMILY = {"car": "light", "truck": "heavy", "bus": "heavy", "motorcycle": "two_wheeler", "bicycle": "two_wheeler"}


def plate_veto(a: str, b: str, p_rel: float) -> str | None:
    """Why two plate reads cannot belong to one vehicle, or ``None``.

    Confident reads (``p_rel`` >= 1) may differ only by OCR-confusable characters: a single
    other substitution is a different registration (sequential plates such as UP32TE4006 /
    UP32TE6006 are common, and appearance cannot tell such cars apart). A read below the
    confidence bar still vetoes when it shares hardly any characters with the other read.
    """
    if a == b:
        return None
    if p_rel >= 1.0 and weighted_edit_distance(a, b) >= 1.0:
        return "two confident plate reads disagree"
    if p_rel >= 0.5 and (plate_similarity(a, b) or 0.0) < 0.5:
        return "plate reads disagree on most characters"
    return None


@dataclass
class Candidate:
    vehicle: GlobalVehicle
    prev: VehicleObservation
    score: float = 0.0
    components: dict[str, float | None] = field(default_factory=dict)
    weights: dict[str, float] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    rejected: str | None = None
    path: PathInfo | None = None
    dt_s: float = 0.0
    new_journey: bool = False
    strong_plate: bool = False


@dataclass
class MatchResult:
    vehicle: GlobalVehicle | None
    prev: VehicleObservation | None
    score: float | None
    confidence_level: str
    reasons: list[str]
    components: dict[str, Any]
    new_journey: bool
    path: PathInfo | None
    dt_s: float | None
    conflicts: list[dict[str, Any]]
    considered: int


def decode_embedding(b64: str | None) -> np.ndarray | None:
    if not b64:
        return None
    try:
        return np.frombuffer(base64.b64decode(b64), dtype=np.float32)
    except (ValueError, TypeError):
        return None


def colour_score(a: str | None, b: str | None) -> float | None:
    if not a or not b or "unknown" in (a, b):
        return None
    if a == b:
        return 1.0
    return 0.4 if {a, b} in SIMILAR_COLOURS else 0.0


def class_score(a: str | None, b: str | None) -> float | None:
    if not a or not b:
        return None
    if a == b:
        return 1.0
    # detector class flips between car/truck/bus are common at a distance
    return 0.4 if VEHICLE_FAMILY.get(a) in ("light", "heavy") and VEHICLE_FAMILY.get(b) in ("light", "heavy") else 0.0


def level_for(score: float, cfg: dict[str, Any]) -> str:
    if score >= cfg["high_confidence"]:
        return "HIGH"
    if score >= cfg["medium_confidence"]:
        return "MEDIUM"
    return "LOW"


class IdentityResolver:
    def __init__(self, db: Session, graph: TopologyGraph, cfg: dict[str, Any], baselines: dict[tuple[str, str], dict]) -> None:
        self.db = db
        self.graph = graph
        self.cfg = cfg
        self.baselines = baselines

    # ------------------------------------------------------------------ candidates
    def _candidates(self, obs: VehicleObservation) -> list[tuple[GlobalVehicle, VehicleObservation]]:
        cfg = self.cfg
        t = obs.observed_at
        window_start = t - timedelta(seconds=cfg["max_link_window_s"])
        q = (select(GlobalVehicle).where(GlobalVehicle.last_seen_at >= window_start, GlobalVehicle.last_seen_at <= t + timedelta(seconds=2),
                                         GlobalVehicle.is_demo == obs.is_demo)
             .order_by(GlobalVehicle.last_seen_at.desc()).limit(cfg["candidate_limit"]))
        vehicles = {v.id: v for v in self.db.scalars(q)}
        if obs.plate_text:  # same plate outside the time window: identity continues as a new journey
            for v in self.db.scalars(select(GlobalVehicle).where(GlobalVehicle.plate_text == obs.plate_text, GlobalVehicle.is_demo == obs.is_demo,
                                                                 GlobalVehicle.last_seen_at <= t + timedelta(seconds=2))
                                     .order_by(GlobalVehicle.last_seen_at.desc()).limit(5)):
                vehicles.setdefault(v.id, v)
        ids = [v.last_observation_id for v in vehicles.values() if v.last_observation_id]
        prevs = {o.id: o for o in self.db.scalars(select(VehicleObservation).where(VehicleObservation.id.in_(ids)))} if ids else {}
        return [(v, prevs[v.last_observation_id]) for v in vehicles.values() if v.last_observation_id in prevs]

    def _embedding(self, obs_id: int) -> tuple[np.ndarray | None, str | None]:
        row = self.db.scalar(select(VehicleEmbedding).where(VehicleEmbedding.observation_id == obs_id))
        if row is None:
            return None, None
        return np.frombuffer(row.vector, dtype=np.float32), row.model

    # ------------------------------------------------------------------ scoring
    def score(self, obs: VehicleObservation, emb: np.ndarray | None, emb_model: str | None, v: GlobalVehicle, prev: VehicleObservation) -> Candidate:
        cfg = self.cfg
        w = cfg["weights"]
        c = Candidate(v, prev)
        dt = (obs.observed_at - prev.observed_at).total_seconds()
        c.dt_s = dt
        if prev.id == obs.id or dt < -2:
            c.rejected = "observation precedes the vehicle's last sighting"
            return c
        dt = max(dt, 0.0)

        # --- plate ---------------------------------------------------------------
        psim = plate_similarity(obs.plate_text, prev.plate_text)
        p_rel = 0.0
        if psim is not None:
            conf = min(obs.plate_confidence or 0.0, prev.plate_confidence or 0.0)
            p_rel = min(1.0, conf / max(1e-6, cfg["plate_min_confidence"]))
            c.components["plate"] = round(psim, 3)
            c.weights["plate"] = w["plate"] * p_rel
            c.strong_plate = psim >= cfg["plate_strong_similarity"] and p_rel >= 1.0
            if psim == 1.0:
                c.reasons.append(f"plate {obs.plate_text} matches exactly (OCR {prev.plate_confidence:.0%} / {obs.plate_confidence:.0%})")
            else:
                how = "differ only by OCR-confusable characters" if weighted_edit_distance(obs.plate_text, prev.plate_text) < 1.0 else "differ"
                c.reasons.append(f"plates {prev.plate_text} / {obs.plate_text} {how} (similarity {psim:.2f})")
                if veto := plate_veto(obs.plate_text, prev.plate_text, p_rel):
                    c.rejected = veto
                    return c

        # the vehicle's best-known plate also counts: a plate-less intermediate sighting
        # must not let a vehicle "change" its confidently read registration
        if obs.plate_text and v.plate_text and v.plate_text != prev.plate_text:
            vsim = plate_similarity(obs.plate_text, v.plate_text) or 0.0
            vconf = min(obs.plate_confidence or 0.0, v.plate_confidence or 0.0)
            if veto := plate_veto(obs.plate_text, v.plate_text, vconf / max(1e-6, cfg["plate_min_confidence"])):
                c.reasons.append(f"plate {obs.plate_text} differs from the vehicle's registered plate {v.plate_text} (similarity {vsim:.2f})")
                c.rejected = veto
                return c

        # --- topology / time ------------------------------------------------------
        path = self.graph.shortest(prev.camera_id, obs.camera_id)
        c.path = path
        if path is None:
            if not c.strong_plate:
                c.rejected = f"no allowed road path {prev.camera_id} → {obs.camera_id}"
                return c
            c.new_journey = True
            c.reasons.append(f"no allowed road path {prev.camera_id} → {obs.camera_id}; identity kept by plate as a new journey")
        else:
            min_t = path.min_travel_s * (1 - cfg["time_tolerance"])
            if dt < min_t:
                c.rejected = (f"physically impossible: {dt:.0f}s from {prev.camera_id} to {obs.camera_id} "
                              f"but the shortest road path ({path.distance_m / 1000:.2f} km) needs ≥ {path.min_travel_s:.0f}s")
                return c
            if dt > cfg["journey_gap_s"] or path.hops > 2:
                c.new_journey = True
            expected = path_baseline_s(self.baselines, path.cameras, path.typical_travel_s)
            ratio = dt / max(expected, 1.0)
            t_score = 1.0 if ratio <= 1.0 else math.exp(-(math.log(ratio) ** 2) / (2 * 0.8 ** 2))
            if not c.new_journey:
                c.components["time"] = round(t_score, 3)
                c.weights["time"] = w["time"]
                c.reasons.append(f"travel time {dt:.0f}s vs expected {expected:.0f}s (min {path.min_travel_s:.0f}s) over {path.distance_m / 1000:.2f} km")
                if path.hops == 1 and path.cameras[0] != path.cameras[-1]:
                    r_score = 1.0
                    c.reasons.append(f"{prev.camera_id} → {obs.camera_id} are adjacent on the road graph")
                elif path.cameras[0] == path.cameras[-1]:
                    r_score = 0.6
                    c.reasons.append(f"returned to {obs.camera_id} via a loop ({' → '.join(path.cameras)})")
                else:
                    r_score = max(0.2, 1.0 - 0.3 * len(path.skipped))
                    c.reasons.append(f"route skips camera(s) {', '.join(path.skipped)} (possible missed detection)")
                c.components["route"] = round(r_score, 3)
                c.weights["route"] = w["route"]
            else:
                c.reasons.append(f"seen again after {dt / 60:.0f} min ({path.hops} hops): new journey")

        if c.new_journey and not c.strong_plate:
            c.rejected = "no confident plate to carry identity across journeys"
            return c

        # --- appearance -------------------------------------------------------------
        if emb is not None:
            pemb, pmodel = self._embedding(prev.id)
            if pemb is not None and pmodel == emb_model and pemb.shape == emb.shape:
                cos = float(np.dot(emb, pemb) / (np.linalg.norm(emb) * np.linalg.norm(pemb) + 1e-9))
                lo, hi = cfg["appearance_floor"], cfg["appearance_ceiling"]
                a_score = min(1.0, max(0.0, (cos - lo) / max(1e-6, hi - lo)))
                c.components["appearance"] = round(a_score, 3)
                c.components["appearance_cosine"] = round(cos, 4)
                c.weights["appearance"] = w["appearance"] * (1.0 if emb_model != "color_histogram" else 0.6)
                c.reasons.append(f"appearance similarity {cos:.3f} ({emb_model})")

        # --- attributes ---------------------------------------------------------------
        parts = [s for s in (class_score(obs.vehicle_class, prev.vehicle_class), colour_score(obs.vehicle_color, prev.vehicle_color)) if s is not None]
        if parts:
            c.components["attributes"] = round(sum(parts) / len(parts), 3)
            c.weights["attributes"] = w["attributes"]
            c.reasons.append(f"class {prev.vehicle_class}/{obs.vehicle_class}, colour {prev.vehicle_color}/{obs.vehicle_color}")

        total_w = sum(c.weights.values())
        if total_w <= 0:
            c.rejected = "no comparable signals"
            return c
        c.score = sum(c.weights[k] * float(c.components[k]) for k in c.weights) / total_w
        # a link needs at least one identifying signal, not just plausible timing
        ident = max(psim if (psim is not None and p_rel >= 0.5) else 0.0, float(c.components.get("appearance") or 0.0))
        if ident < cfg["min_signal_for_link"]:
            c.rejected = f"no identifying signal strong enough (best {ident:.2f} < {cfg['min_signal_for_link']:.2f})"
        return c

    # ------------------------------------------------------------------ decision
    def resolve(self, obs: VehicleObservation, emb: np.ndarray | None, emb_model: str | None) -> MatchResult:
        cfg = self.cfg
        cands = [self.score(obs, emb, emb_model, v, prev) for v, prev in self._candidates(obs) if prev.id != obs.id]
        conflicts = []
        for c in cands:
            if (c.rejected and c.rejected.startswith("physically impossible") and obs.plate_text and c.prev.plate_text == obs.plate_text
                    and c.prev.camera_id != obs.camera_id):
                conflicts.append({"vehicle_id": c.vehicle.id, "vehicle_code": c.vehicle.code, "observation_id": c.prev.id,
                                  "camera_id": c.prev.camera_id, "observed_at": c.prev.observed_at.isoformat(), "dt_s": round(c.dt_s, 1),
                                  "min_travel_s": round(c.path.min_travel_s, 1) if c.path else None,
                                  "distance_m": round(c.path.distance_m, 1) if c.path else None,
                                  "plate_confidence": [c.prev.plate_confidence, obs.plate_confidence],
                                  "vehicle_class": [c.prev.vehicle_class, obs.vehicle_class],
                                  "vehicle_color": [c.prev.vehicle_color, obs.vehicle_color], "reason": c.rejected})
        ok = sorted((c for c in cands if not c.rejected and c.score >= cfg["accept_threshold"]), key=lambda c: c.score, reverse=True)
        considered = len(cands)
        if not ok:
            reasons = ["no existing vehicle matched: registered as a new vehicle"]
            if considered:
                best = max(cands, key=lambda c: c.score)
                why = best.rejected or f"best score {best.score:.2f} below threshold {cfg['accept_threshold']:.2f}"
                reasons.append(f"{considered} candidate(s) considered; closest {best.vehicle.code}: {why}")
            return MatchResult(None, None, None, "NEW", reasons, {}, True, None, None, conflicts, considered)
        best = ok[0]
        reasons = list(best.reasons)
        level = level_for(best.score, cfg)
        cap = cfg.get("unconfirmed_plate_max_level", "MEDIUM")
        if level == "HIGH" and not best.strong_plate and cap != "HIGH":
            # appearance + timing alone cannot tell two similar vehicles apart: never report it as certain
            level = cap
            reasons.append(f"no confident plate corroboration: confidence capped at {cap}")
        if len(ok) > 1:
            margin = best.score - ok[1].score
            if margin < cfg["ambiguity_margin"] and not best.strong_plate:
                reasons.append(f"ambiguous: {ok[1].vehicle.code} scores {ok[1].score:.2f} (margin {margin:.2f}); not linked")
                return MatchResult(None, None, None, "NEW", ["no unambiguous match: registered as a new vehicle"] + reasons, {}, True, None, None, conflicts, considered)
            if margin < cfg["ambiguity_margin"]:
                level = "LOW" if level == "MEDIUM" else level
                reasons.append(f"runner-up {ok[1].vehicle.code} scores {ok[1].score:.2f}")
        comps = {"scores": best.components, "weights": {k: round(v, 3) for k, v in best.weights.items()}, "fused": round(best.score, 4),
                 "path": best.path.as_dict() if best.path else None, "dt_s": round(best.dt_s, 1), "runner_up": round(ok[1].score, 4) if len(ok) > 1 else None}
        return MatchResult(best.vehicle, best.prev, round(best.score, 4), level, reasons, comps, best.new_journey, best.path, best.dt_s, conflicts, considered)
