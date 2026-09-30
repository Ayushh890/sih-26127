"""Evidence packages (vehicle/plate crops and before/detection/after frames) with SHA-256
integrity verification. Access requires ``evidence:read`` and is audited."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.api.deps import CurrentUser, client_ip, not_found, protect, require
from app.auth.permissions import Perm
from app.db.models import Evidence
from app.db.session import get_db
from app.evidence.store import ROLES, get_evidence_store
from app.services import audit
from app.services.views import evidence_dict

router = APIRouter(prefix="/api/evidence", tags=["evidence"])


def _get(db: Session, evidence_id: str) -> Evidence:
    if len(evidence_id) > 36:
        raise not_found("evidence")
    e = db.get(Evidence, evidence_id)
    if e is None:
        raise not_found("evidence")
    return e


@router.get("/{evidence_id}", summary="Evidence metadata and file hashes")
def meta(evidence_id: str, request: Request, user: CurrentUser = Depends(require(Perm.EVIDENCE_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    e = _get(db, evidence_id)
    audit.record(db, user.as_dict(), "evidence.view", "evidence", e.id, {"observation_id": e.observation_id}, client_ip(request))
    d = evidence_dict(e)
    d["urls"] = {role: f"/api/evidence/{e.id}/{role}.jpg" for role in (e.files or {})}
    return protect(db, user, d)


@router.get("/{evidence_id}/{role}.jpg", summary="Evidence image (vehicle, plate, before, detection, after)",
            responses={200: {"content": {"image/jpeg": {}}}})
def image(evidence_id: str, role: str, request: Request, user: CurrentUser = Depends(require(Perm.EVIDENCE_READ)),
          db: Session = Depends(get_db)) -> Response:
    if role not in ROLES:
        raise not_found("evidence file")
    e = _get(db, evidence_id)
    f = (e.files or {}).get(role)
    if not f:
        raise not_found("evidence file")
    try:
        data = get_evidence_store().read_file(f["path"], e.encrypted)
    except FileNotFoundError:
        raise HTTPException(status.HTTP_410_GONE, "evidence file no longer on disk (retention or manual deletion)") from None
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    audit.record(db, user.as_dict(), "evidence.download", "evidence", e.id, {"file": role}, client_ip(request))
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300", "X-Evidence-SHA256": f.get("sha256", "")})


@router.post("/{evidence_id}/verify", summary="Re-hash files and manifest to check integrity")
def verify(evidence_id: str, request: Request, user: CurrentUser = Depends(require(Perm.EVIDENCE_READ)), db: Session = Depends(get_db)) -> dict[str, Any]:
    e = _get(db, evidence_id)
    r = get_evidence_store().verify(e.files or {}, e.manifest_sha256)
    audit.record(db, user.as_dict(), "evidence.verify", "evidence", e.id, {"valid": r["valid"]}, client_ip(request))
    return {"evidence_id": e.id, "manifest_sha256": e.manifest_sha256, **r}
