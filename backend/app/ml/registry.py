"""Model registry: the single place that knows model paths and backends.

Stateless ONNX models are shared across cameras (ONNX Runtime sessions are
thread-safe for ``run``). Stateful fallbacks (motion detector) are created per
camera. ``status()`` reports what is actually loaded — including when a
fallback is in use — so the UI never over-states capabilities.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.config import get_settings
from app.core.logging import get_logger
from app.ml.detectors.base import PlateDetector, VehicleDetector
from app.ml.detectors.plate import ContourPlateDetector, YoloV9PlateDetector
from app.ml.detectors.vehicle import MotionDetector, YoloxOnnxDetector
from app.ml.ocr.engine import CCTPlateOCR, PlateOCR
from app.ml.reid.embedder import ColorHistogramReIdentifier, MobileNetReIdentifier, VehicleReIdentifier

log = get_logger("ml.registry")


@dataclass
class ModelStatus:
    role: str
    configured: str
    active: str | None
    path: str | None
    fallback: bool
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class ModelRegistry:
    config: dict[str, Any]
    models_dir: Path
    device: str
    threads: int
    _vehicle: VehicleDetector | None = None
    _vehicle_factory_motion: bool = False
    plate: PlateDetector | None = None
    ocr: PlateOCR | None = None
    reid: VehicleReIdentifier | None = None
    statuses: dict[str, ModelStatus] = field(default_factory=dict)

    # ------------------------------------------------------------------ loading
    def _path(self, section: dict[str, Any], key: str = "path") -> Path | None:
        val = section.get(key)
        if not val:
            return None
        p = Path(val)
        return p if p.is_absolute() else self.models_dir / p

    def load(self) -> "ModelRegistry":
        self._load_vehicle()
        self._load_plate()
        self._load_ocr()
        self._load_reid()
        return self

    def _load_vehicle(self) -> None:
        sec = self.config.get("vehicle_detector", {})
        path = self._path(sec)
        try:
            if sec.get("backend") == "yolox_onnx" and path and path.exists():
                self._vehicle = YoloxOnnxDetector(path, int(sec.get("input_size", 416)), sec.get("classes", {}), float(sec.get("nms_iou", 0.45)), self.device, self.threads)
                self.statuses["vehicle_detector"] = ModelStatus("vehicle_detector", "yolox_onnx", "yolox_onnx", str(path), False)
                return
            if sec.get("backend") == "motion":
                self._vehicle_factory_motion = True
                self.statuses["vehicle_detector"] = ModelStatus("vehicle_detector", "motion", "motion", None, False)
                return
            raise FileNotFoundError(f"model not found: {path}")
        except Exception as exc:
            log.warning("vehicle detector unavailable (%s); falling back to %s", exc, sec.get("fallback", "motion"))
            self._vehicle_factory_motion = True
            self.statuses["vehicle_detector"] = ModelStatus("vehicle_detector", str(sec.get("backend")), "motion", None, True, str(exc))

    def _load_plate(self) -> None:
        sec = self.config.get("plate_detector", {})
        path = self._path(sec)
        try:
            if sec.get("backend") == "yolov9_onnx" and path and path.exists():
                self.plate = YoloV9PlateDetector(path, int(sec.get("input_size", 384)), self.device, self.threads)
                self.statuses["plate_detector"] = ModelStatus("plate_detector", "yolov9_onnx", "yolov9_onnx", str(path), False)
                return
            if sec.get("backend") == "contour":
                self.plate = ContourPlateDetector()
                self.statuses["plate_detector"] = ModelStatus("plate_detector", "contour", "contour", None, False)
                return
            raise FileNotFoundError(f"model not found: {path}")
        except Exception as exc:
            log.warning("plate detector unavailable (%s); falling back to contour detector", exc)
            self.plate = ContourPlateDetector()
            self.statuses["plate_detector"] = ModelStatus("plate_detector", str(sec.get("backend")), "contour", None, True, str(exc))

    def _load_ocr(self) -> None:
        sec = self.config.get("ocr", {})
        path, cfg = self._path(sec), self._path(sec, "config")
        try:
            if path and cfg and path.exists() and cfg.exists():
                self.ocr = CCTPlateOCR(path, cfg, self.device, self.threads)
                self.statuses["ocr"] = ModelStatus("ocr", str(sec.get("backend")), "cct_onnx", str(path), False)
                return
            raise FileNotFoundError(f"model not found: {path}")
        except Exception as exc:
            log.error("OCR model unavailable (%s): plates will not be read. Run scripts/download_models.py", exc)
            self.ocr = None
            self.statuses["ocr"] = ModelStatus("ocr", str(sec.get("backend")), None, None, True, str(exc))

    def _load_reid(self) -> None:
        sec = self.config.get("reid", {})
        path = self._path(sec)
        try:
            if sec.get("backend") == "mobilenetv2_onnx" and path and path.exists():
                self.reid = MobileNetReIdentifier(path, int(sec.get("input_size", 224)), self.device, self.threads)
                self.statuses["reid"] = ModelStatus("reid", "mobilenetv2_onnx", "mobilenetv2_onnx", str(path), False)
                return
            if sec.get("backend") == "color_histogram":
                self.reid = ColorHistogramReIdentifier()
                self.statuses["reid"] = ModelStatus("reid", "color_histogram", "color_histogram", None, False)
                return
            raise FileNotFoundError(f"model not found: {path}")
        except Exception as exc:
            log.warning("re-id model unavailable (%s); falling back to colour histograms", exc)
            self.reid = ColorHistogramReIdentifier()
            self.statuses["reid"] = ModelStatus("reid", str(sec.get("backend")), "color_histogram", None, True, str(exc))

    # ------------------------------------------------------------------ access
    def vehicle_detector_for_camera(self) -> VehicleDetector:
        if self._vehicle is not None and not self._vehicle_factory_motion:
            return self._vehicle
        return MotionDetector()

    @property
    def reid_model_name(self) -> str:
        return self.reid.name if self.reid else "none"

    def status(self) -> list[dict[str, Any]]:
        return [s.as_dict() for s in self.statuses.values()]


_registry: ModelRegistry | None = None
_lock = threading.Lock()


def load_model_config(path: Path | None = None) -> dict[str, Any]:
    p = path or get_settings().MODEL_CONFIG
    if not p.exists():
        log.warning("model config %s missing; using fallbacks only", p)
        return {}
    return yaml.safe_load(p.read_text()) or {}


def get_registry() -> ModelRegistry:
    global _registry
    with _lock:
        if _registry is None:
            s = get_settings()
            cfg = load_model_config()
            device = s.INFERENCE_DEVICE if s.INFERENCE_DEVICE != "auto" else cfg.get("device", "auto")
            _registry = ModelRegistry(config=cfg, models_dir=s.MODELS_DIR, device=device, threads=s.inference_threads).load()
        return _registry


def set_registry(reg: ModelRegistry | None) -> None:
    global _registry
    with _lock:
        _registry = reg
