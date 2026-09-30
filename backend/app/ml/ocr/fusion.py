"""Temporal OCR fusion: combine per-frame plate reads of one track into a single result.

Reads are grouped by length (the modal length wins, weighted by confidence);
within that group every character position is decided by a confidence-weighted
vote. Reads matching a valid Indian format get extra weight. The fused
confidence reflects both per-character agreement and length agreement, so a
track whose frames disagree gets a visibly lower confidence.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field


@dataclass
class PlateReadSample:
    raw: str
    normalized: str
    confidence: float
    char_confidences: list[float] = field(default_factory=list)
    is_valid: bool = False
    ts: float = 0.0


@dataclass
class FusedPlate:
    text: str
    confidence: float
    votes: int  # reads that agree exactly with the fused text
    total_reads: int
    is_valid: bool
    raw_best: str  # raw OCR text of the best supporting read
    position_confidence: list[float]


VALID_BONUS = 1.5
# The shortest Indian registration has 6 characters (e.g. old "DL1C12"); shorter reads are
# fragments of an occluded or badly cropped plate. They stay stored as raw plate reads
# (evidence) but never vote on, or become, the vehicle's plate.
MIN_PLATE_CHARS = 6


def fuse_reads(reads: list[PlateReadSample]) -> FusedPlate | None:
    usable = [r for r in reads if len(r.normalized) >= MIN_PLATE_CHARS]
    if not usable:
        return None
    weight = lambda r: max(r.confidence, 1e-3) * (VALID_BONUS if r.is_valid else 1.0)  # noqa: E731

    by_len: dict[int, float] = defaultdict(float)
    for r in usable:
        by_len[len(r.normalized)] += weight(r)
    total_w = sum(by_len.values())
    length = max(by_len.items(), key=lambda kv: kv[1])[0]
    group = [r for r in usable if len(r.normalized) == length]
    length_agreement = by_len[length] / total_w

    chars: list[str] = []
    pos_conf: list[float] = []
    for i in range(length):
        scores: dict[str, float] = defaultdict(float)
        conf_sum: dict[str, float] = defaultdict(float)
        count: dict[str, int] = defaultdict(int)
        for r in group:
            c = r.normalized[i]
            cc = r.char_confidences[i] if i < len(r.char_confidences) else r.confidence
            scores[c] += weight(r) * max(cc, 1e-3)
            conf_sum[c] += cc
            count[c] += 1
        winner = max(scores.items(), key=lambda kv: kv[1])[0]
        agreement = scores[winner] / sum(scores.values())
        pos_conf.append(agreement * (conf_sum[winner] / count[winner]))
        chars.append(winner)

    text = "".join(chars)
    agreeing = [r for r in usable if r.normalized == text]
    best = max(agreeing or group, key=lambda r: r.confidence)
    # a single read can't be corroborated: cap its confidence
    corroboration = min(1.0, 0.75 + 0.125 * (len(agreeing) - 1)) if agreeing else 0.6
    confidence = (sum(pos_conf) / len(pos_conf)) * length_agreement * corroboration
    from app.ml.ocr.normalize import is_valid_plate

    return FusedPlate(
        text=text,
        confidence=round(float(confidence), 4),
        votes=len(agreeing),
        total_reads=len(usable),
        is_valid=is_valid_plate(text),
        raw_best=best.raw,
        position_confidence=[round(float(p), 4) for p in pos_conf],
    )
