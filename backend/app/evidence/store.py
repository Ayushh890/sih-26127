"""Evidence packages: vehicle/plate crops and before/detection/after frames.

Layout: ``DATA_DIR/evidence/YYYY/MM/DD/<uuid>/`` containing JPEG files (``.jpg`` or
``.jpg.enc`` when encryption at rest is enabled) plus ``manifest.json``. Every file's
SHA-256 (of the bytes as stored) is recorded in the manifest, and the manifest's own
SHA-256 is stored in the database, so tampering with either is detectable.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from app.core.config import get_settings
from app.core.logging import get_logger
from app.core.security import decrypt_bytes, encrypt_bytes, sha256_bytes

log = get_logger("evidence")

ROLES = ("vehicle", "plate", "before", "detection", "after")


def _canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str).encode()


def _resize(img: np.ndarray, width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if w <= width:
        return img
    return cv2.resize(img, (width, max(1, int(h * width / w))), interpolation=cv2.INTER_AREA)


class EvidenceStore:
    def __init__(self, root: Path | None = None, encrypt: bool | None = None, max_mb: int | None = None) -> None:
        s = get_settings()
        self.root = Path(root or s.evidence_dir)
        self.encrypt = s.EVIDENCE_ENCRYPTION if encrypt is None else encrypt
        self.max_bytes = (s.EVIDENCE_MAX_MB if max_mb is None else max_mb) * 1024 * 1024
        self.quality = s.EVIDENCE_JPEG_QUALITY
        self._lock = threading.Lock()
        self._size: int | None = None
        self._last_full_warning = 0.0
        self.written = 0
        self.skipped_full = 0

    # ------------------------------------------------------------------ disk guard
    def usage_bytes(self, refresh: bool = False) -> int:
        with self._lock:
            if self._size is None or refresh:
                total = 0
                if self.root.exists():
                    for dirpath, _, files in os.walk(self.root):
                        for f in files:
                            try:
                                total += os.path.getsize(os.path.join(dirpath, f))
                            except OSError:
                                pass
                self._size = total
            return self._size

    def _account(self, n: int) -> None:
        with self._lock:
            self._size = (self._size or 0) + n

    def is_full(self) -> bool:
        return self.max_bytes > 0 and self.usage_bytes() >= self.max_bytes

    # ------------------------------------------------------------------ writing
    def _encode(self, img: np.ndarray) -> bytes:
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.quality])
        if not ok:
            raise ValueError("jpeg encoding failed")
        data = buf.tobytes()
        return encrypt_bytes(data) if self.encrypt else data

    def write(self, camera_id: str, event: dict[str, Any], st: Any, cfg: Any) -> dict[str, Any] | None:
        """Pipeline hook: persist the evidence for a finalised track, return its manifest."""
        images: dict[str, np.ndarray] = {}
        scale_w = int(getattr(cfg, "evidence_frame_width", 640))
        if st.best_crop is not None and st.best_crop[1].size:
            images["vehicle"] = st.best_crop[1]
        if st.best_plate is not None and st.best_plate[1].size:
            images["plate"] = st.best_plate[1]
        if getattr(cfg, "evidence_full_frames", True):
            det = st.frame_detection  # frame of the best plate read (None if no plate was read)
            if det is not None:
                images["detection"] = self._annotate(det, event.get("bbox"), st.best_plate[2] if st.best_plate else None, scale_w)
            if st.frame_before is not None:
                images["before"] = _resize(st.frame_before, scale_w)
            if st.frame_after is not None:
                images["after"] = _resize(st.frame_after, scale_w)
        meta = {
            "camera_id": camera_id,
            "event_id": event.get("event_id"),
            "local_track_id": event.get("local_track_id"),
            "observed_at": event.get("observed_at"),
            "bbox": event.get("bbox"),
            "plate_bbox": st.best_plate[2] if st.best_plate else None,
            "ocr_text": event.get("plate_text"),
            "ocr_raw": event.get("plate_raw"),
            "ocr_confidence": event.get("plate_confidence"),
            "is_demo": bool(event.get("is_demo")),
        }
        return self.write_images(camera_id, images, meta, ts=event.get("observed_at"))

    def write_images(self, camera_id: str, images: dict[str, np.ndarray], meta: dict[str, Any], ts: float | None = None) -> dict[str, Any] | None:
        if not images:
            return None
        if self.is_full():
            self.skipped_full += 1
            if time.time() - self._last_full_warning > 60:
                self._last_full_warning = time.time()
                log.warning("evidence storage limit reached; new evidence is not being written", extra={"camera_id": camera_id})
            return None
        when = datetime.fromtimestamp(ts or time.time(), tz=timezone.utc)
        eid = str(uuid.uuid4())
        rel_dir = Path(when.strftime("%Y/%m/%d")) / eid
        out_dir = self.root / rel_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, dict[str, Any]] = {}
        total = 0
        for role in ROLES:
            img = images.get(role)
            if img is None or not img.size:
                continue
            data = self._encode(img)
            name = f"{role}.jpg" + (".enc" if self.encrypt else "")
            (out_dir / name).write_bytes(data)
            total += len(data)
            files[role] = {"path": str(rel_dir / name), "sha256": sha256_bytes(data), "bytes": len(data), "width": int(img.shape[1]), "height": int(img.shape[0])}
        manifest = {"id": eid, "created_at": datetime.now(timezone.utc).isoformat(), "ts": when.isoformat(), "encrypted": self.encrypt, "files": files, **meta}
        mbytes = _canonical(manifest)
        (out_dir / "manifest.json").write_bytes(mbytes)
        total += len(mbytes)
        self._account(total)
        self.written += 1
        return {**manifest, "manifest_sha256": sha256_bytes(mbytes)}

    @staticmethod
    def _annotate(frame: np.ndarray, bbox: list[float] | None, plate_bbox: list[float] | None, width: int) -> np.ndarray:
        out = frame.copy()
        if bbox:
            x1, y1, x2, y2 = (int(v) for v in bbox)
            cv2.rectangle(out, (x1, y1), (x2, y2), (80, 220, 80), 3)
        if plate_bbox:
            x1, y1, x2, y2 = (int(v) for v in plate_bbox)
            cv2.rectangle(out, (x1, y1), (x2, y2), (0, 170, 255), 3)
        return _resize(out, width)

    # ------------------------------------------------------------------ reading / verification
    def _resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise ValueError("evidence path escapes the evidence root")
        return p

    def read_file(self, rel: str, encrypted: bool) -> bytes:
        data = self._resolve(rel).read_bytes()
        return decrypt_bytes(data) if encrypted else data

    def verify(self, files: dict[str, dict[str, Any]], manifest_sha256: str | None) -> dict[str, Any]:
        """Recompute file and manifest hashes; returns per-file status."""
        results: dict[str, Any] = {}
        ok = True
        manifest_dir: Path | None = None
        for role, f in files.items():
            try:
                p = self._resolve(f["path"])
                manifest_dir = p.parent
                actual = sha256_bytes(p.read_bytes())
                good = actual == f["sha256"]
                results[role] = {"ok": good, "expected": f["sha256"], "actual": actual}
            except FileNotFoundError:
                good = False
                results[role] = {"ok": False, "expected": f.get("sha256"), "actual": None, "error": "missing"}
            except ValueError as e:
                good = False
                results[role] = {"ok": False, "error": str(e)}
            ok = ok and good
        manifest_ok = None
        if manifest_sha256 and manifest_dir is not None:
            mp = manifest_dir / "manifest.json"
            manifest_ok = mp.exists() and sha256_bytes(mp.read_bytes()) == manifest_sha256
            ok = ok and bool(manifest_ok)
        return {"valid": ok, "manifest_valid": manifest_ok, "files": results}

    def delete(self, files: dict[str, dict[str, Any]]) -> int:
        freed = 0
        dirs: set[Path] = set()
        for f in files.values():
            try:
                p = self._resolve(f["path"])
                dirs.add(p.parent)
                freed += p.stat().st_size
                p.unlink()
            except (FileNotFoundError, ValueError):
                pass
        for d in dirs:
            m = d / "manifest.json"
            if m.exists():
                freed += m.stat().st_size
                m.unlink()
            try:
                d.rmdir()
            except OSError:
                pass
        self._account(-freed)
        return freed


_store: EvidenceStore | None = None


def get_evidence_store() -> EvidenceStore:
    global _store
    if _store is None:
        _store = EvidenceStore()
    return _store


def set_evidence_store(store: EvidenceStore | None) -> None:
    global _store
    _store = store
