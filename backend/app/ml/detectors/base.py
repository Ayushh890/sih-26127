from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class Detection:
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float
    label: str

    @property
    def xyxy(self) -> np.ndarray:
        return np.array([self.x1, self.y1, self.x2, self.y2], dtype=np.float32)

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    def clip(self, w: int, h: int) -> "Detection":
        return Detection(max(0.0, self.x1), max(0.0, self.y1), min(float(w), self.x2), min(float(h), self.y2), self.confidence, self.label)


class VehicleDetector(ABC):
    """Detects vehicles in a full frame. Implementations must be safe to call from one thread per camera."""

    name: str = "abstract"

    @abstractmethod
    def detect(self, frame: np.ndarray, conf_threshold: float) -> list[Detection]: ...


class PlateDetector(ABC):
    """Detects licence plates inside a vehicle crop."""

    name: str = "abstract"

    @abstractmethod
    def detect(self, crop: np.ndarray, conf_threshold: float) -> list[Detection]: ...


def letterbox(img: np.ndarray, size: int, center: bool, pad_value: int = 114) -> tuple[np.ndarray, float, tuple[float, float]]:
    h, w = img.shape[:2]
    r = min(size / h, size / w)
    nw, nh = int(round(w * r)), int(round(h * r))
    resized = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_LINEAR)
    out = np.full((size, size, 3), pad_value, dtype=np.uint8)
    dx = (size - nw) / 2 if center else 0.0
    dy = (size - nh) / 2 if center else 0.0
    ix, iy = int(round(dx - 0.1)), int(round(dy - 0.1))
    out[iy : iy + nh, ix : ix + nw] = resized
    return out, r, (ix, iy)


def nms(dets: list[Detection], iou: float) -> list[Detection]:
    if not dets:
        return []
    boxes = [[d.x1, d.y1, d.width, d.height] for d in dets]
    scores = [d.confidence for d in dets]
    keep = cv2.dnn.NMSBoxes(boxes, scores, 0.0, iou)
    return [dets[int(i)] for i in np.array(keep).reshape(-1)]
