"""Privacy masking.

Roles without ``plates:view_raw`` (analyst, viewer by default; configurable in the
``privacy`` settings section) never receive raw plate text: plates are replaced by a
stable keyed pseudonym (``PSN-…``) so analytics remain linkable without revealing the
registration. Authorised operators see the underlying plate and evidence.
"""
from __future__ import annotations

import re
from typing import Any

from app.auth.permissions import Perm, has_perm
from app.core.security import plate_pseudonym

PLATE_KEYS = {"plate_text", "plate_raw", "plate_display", "raw_text", "normalized_text", "plate", "plates", "ocr_text", "ocr_raw", "plate_query"}
SENSITIVE_DROP = {"plate_reads_raw"}
_PLATE_RE = re.compile(r"\b(?:[A-Z]{2}\s?\d{1,2}\s?[A-Z]{0,3}\s?\d{3,4}|\d{2}\s?BH\s?\d{4}\s?[A-Z]{1,2})\b")


def can_view_raw(role: str, privacy_cfg: dict[str, Any] | None = None) -> bool:
    if privacy_cfg is not None and role in (privacy_cfg.get("pseudonymise_for_roles") or []):
        return False
    return has_perm(role, Perm.PLATES_VIEW_RAW)


def _pseudo(value: str) -> str | None:
    # display forms ("UP 32 AB 1234") map to the same pseudonym as the normalised plate
    return value if value.startswith("PSN-") else plate_pseudonym(value.replace(" ", ""))


def mask_text(text: str | None) -> str | None:
    if not text:
        return text
    return _PLATE_RE.sub(lambda m: plate_pseudonym(m.group(0).replace(" ", "")) or "", text)


def pseudonym_prefix(query: str) -> str | None:
    """``PSN-ABCDEF1234`` -> lower-case plate-hash prefix for searching by pseudonym."""
    q = query.strip().upper()
    if q.startswith("PSN-") and 4 <= len(q) - 4 <= 10 and all(c in "0123456789ABCDEF" for c in q[4:]):
        return q[4:].lower()
    return None


def mask(obj: Any) -> Any:
    """Recursively pseudonymise plate fields and plate-like substrings in free text."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in SENSITIVE_DROP:
                continue
            if k in PLATE_KEYS and isinstance(v, str):
                out[k] = _pseudo(v)
            elif k in PLATE_KEYS and isinstance(v, list):
                out[k] = [_pseudo(x) if isinstance(x, str) else x for x in v]
            else:
                out[k] = mask(v)
        return out
    if isinstance(obj, list):
        return [mask(v) for v in obj]
    if isinstance(obj, str):
        return mask_text(obj)
    return obj


def apply(obj: Any, role: str, privacy_cfg: dict[str, Any] | None = None) -> Any:
    return obj if can_view_raw(role, privacy_cfg) else mask(obj)
