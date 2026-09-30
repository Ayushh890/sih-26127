"""Indian registration-plate normalisation and similarity.

The normaliser never discards the OCR output: :class:`NormalizedPlate` keeps the
raw text, the cleaned text, the normalised text and an explicit list of every
character that was coerced (position, from, to, reason), so an operator can
always see what the OCR actually produced.

Supported formats
-----------------
* Standard:  ``SS DD L{0,3} DDDD``  e.g. UP32AB1234, DL01XY4567, MH12A1234
* Delhi single-digit district: ``DL 3 C AB 1234`` -> DL3CAB1234
* Bharat series: ``YY BH DDDD L{1,2}`` e.g. 22BH1234AA
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache

STATE_CODES = frozenset(
    "AN AP AR AS BR CH CG DD DL DN GA GJ HR HP JK JH KA KL LA LD MP MH MN ML MZ NL OD OR PY PB RJ SK TN TS TR UP UK UA WB".split()
)

# OCR confusions: letter read where a digit is expected, and vice versa.
LETTER_TO_DIGIT = {"O": "0", "D": "0", "Q": "0", "U": "0", "I": "1", "L": "1", "J": "1", "Z": "2", "S": "5", "B": "8", "G": "6", "A": "4", "T": "7"}
DIGIT_TO_LETTER = {"0": "O", "1": "I", "2": "Z", "5": "S", "8": "B", "6": "G", "4": "A", "7": "T", "3": "B"}

# pairs that OCR frequently confuses; cheaper substitutions in the similarity metric
_CONFUSABLE = [
    ("0", "O"), ("0", "D"), ("0", "Q"), ("O", "D"), ("O", "Q"), ("1", "I"), ("1", "L"), ("1", "7"), ("I", "L"),
    ("5", "S"), ("8", "B"), ("2", "Z"), ("6", "G"), ("4", "A"), ("7", "T"), ("3", "8"), ("6", "8"), ("M", "N"),
    ("M", "H"), ("U", "V"), ("P", "R"), ("C", "G"), ("E", "F"), ("K", "X"), ("W", "V"), ("Y", "V"),
]
CONFUSION_COST = {frozenset(p): 0.35 for p in _CONFUSABLE}

# L = letter, D = digit. Order expresses preference when costs tie.
TEMPLATES: tuple[tuple[str, str], ...] = (
    ("standard", "LLDDLLDDDD"),
    ("standard", "LLDDLDDDD"),
    ("standard", "LLDDLLLDDDD"),
    ("standard", "LLDDDDDD"),
    ("delhi", "LLDLLLDDDD"),
    ("delhi", "LLDLLDDDD"),
    ("standard", "LLDDLLDDD"),
    ("standard", "LLDDLDDD"),
    ("bharat", "DDLLDDDDLL"),
    ("bharat", "DDLLDDDDL"),
)

STANDARD_RE = re.compile(r"^(?P<state>[A-Z]{2})(?P<district>\d{1,2})(?P<series>[A-Z]{0,3})(?P<number>\d{3,4})$")
BHARAT_RE = re.compile(r"^(?P<year>\d{2})BH(?P<number>\d{4})(?P<series>[A-Z]{1,2})$")


@dataclass
class Correction:
    position: int
    original: str
    corrected: str
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {"position": self.position, "from": self.original, "to": self.corrected, "reason": self.reason}


@dataclass
class NormalizedPlate:
    raw: str
    cleaned: str
    normalized: str
    is_valid: bool
    format: str | None
    corrections: list[Correction] = field(default_factory=list)

    @property
    def display(self) -> str:
        return format_plate(self.normalized)


def clean(raw: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]", "", raw or "").upper()
    if text.startswith("IND") and len(text) > 8:
        text = text[3:]
    return text


def _coerce(ch: str, kind: str) -> tuple[str, float]:
    """Return (character, cost) to make ``ch`` fit ``kind`` ('L' or 'D')."""
    if kind == "D":
        if ch.isdigit():
            return ch, 0.0
        if ch in LETTER_TO_DIGIT:
            return LETTER_TO_DIGIT[ch], 1.0
        return ch, 99.0
    if ch.isalpha():
        return ch, 0.0
    if ch in DIGIT_TO_LETTER:
        return DIGIT_TO_LETTER[ch], 1.0
    return ch, 99.0


def _fix_state(chars: list[str], corrections: list[Correction]) -> None:
    code = "".join(chars[:2])
    if code in STATE_CODES:
        return
    alt = {"0": "D", "O": "D", "Q": "O", "8": "B", "5": "S", "1": "L", "I": "L", "V": "U", "F": "P", "R": "P", "H": "N"}
    for i in (0, 1):
        if chars[i] in alt:
            trial = chars.copy()
            trial[i] = alt[chars[i]]
            if "".join(trial[:2]) in STATE_CODES:
                corrections.append(Correction(i, chars[i], trial[i], "state-code"))
                chars[i] = trial[i]
                return


@lru_cache(maxsize=4096)
def _normalize_cached(raw: str) -> NormalizedPlate:
    cleaned = clean(raw)
    best: tuple[float, int, str, list[str], list[Correction]] | None = None
    for order, (fmt, tpl) in enumerate(TEMPLATES):
        if len(tpl) != len(cleaned):
            continue
        cost = 0.0
        chars: list[str] = []
        corr: list[Correction] = []
        for i, (ch, kind) in enumerate(zip(cleaned, tpl)):
            new, c = _coerce(ch, kind)
            cost += c
            if new != ch and c < 99:
                corr.append(Correction(i, ch, new, "digit-expected" if kind == "D" else "letter-expected"))
            chars.append(new)
        if cost >= 99:
            continue
        if fmt != "bharat":
            _fix_state(chars, corr)
            if "".join(chars[:2]) not in STATE_CODES:
                cost += 1.5
            if fmt == "delhi" and "".join(chars[:2]) != "DL":
                cost += 3.0  # single-digit districts are a Delhi convention
        cand = (cost, order, fmt, chars, corr)
        if best is None or (cand[0], cand[1]) < (best[0], best[1]):
            best = cand
    if best is None:
        return NormalizedPlate(raw=raw, cleaned=cleaned, normalized=cleaned, is_valid=False, format=None)
    _, _, fmt, chars, corr = best
    normalized = "".join(chars)
    return NormalizedPlate(
        raw=raw, cleaned=cleaned, normalized=normalized, is_valid=is_valid_plate(normalized), format=fmt, corrections=corr
    )


def normalize_plate(raw: str) -> NormalizedPlate:
    res = _normalize_cached(raw or "")
    # return a copy so callers can't mutate the cached instance
    return NormalizedPlate(res.raw, res.cleaned, res.normalized, res.is_valid, res.format, list(res.corrections))


def is_valid_plate(text: str) -> bool:
    m = STANDARD_RE.match(text)
    if m:
        return m.group("state") in STATE_CODES
    return bool(BHARAT_RE.match(text))


def format_plate(text: str) -> str:
    """Human display format: ``UP 32 AB 1234``."""
    m = STANDARD_RE.match(text or "")
    if m:
        return " ".join(p for p in (m["state"], m["district"], m["series"], m["number"]) if p)
    b = BHARAT_RE.match(text or "")
    if b:
        return f"{b['year']} BH {b['number']} {b['series']}"
    return text or ""


def _sub_cost(a: str, b: str) -> float:
    if a == b:
        return 0.0
    return CONFUSION_COST.get(frozenset((a, b)), 1.0)


def weighted_edit_distance(a: str, b: str) -> float:
    """Levenshtein distance where OCR-confusable substitutions are cheap."""
    n, m = len(a), len(b)
    prev = [float(j) for j in range(m + 1)]
    for i in range(1, n + 1):
        cur = [float(i)] + [0.0] * m
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + _sub_cost(a[i - 1], b[j - 1]))
        prev = cur
    return prev[m]


def plate_similarity(a: str | None, b: str | None) -> float | None:
    """Similarity in [0, 1]; ``None`` when either plate is missing."""
    if not a or not b:
        return None
    if a == b:
        return 1.0
    dist = weighted_edit_distance(a, b)
    return max(0.0, 1.0 - dist / max(len(a), len(b)))
