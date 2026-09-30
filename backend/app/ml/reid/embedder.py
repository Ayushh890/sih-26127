"""Vehicle appearance embeddings for cross-camera re-identification.

Embeddings are L2-normalised float32 vectors; similarity is cosine. Appearance
similarity is *evidence*, never proof of identity: the identity engine fuses it
with plate, timing and topology signals and reports the contribution of each.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import cv2
import numpy as np

from app.ml.runtime import get_session

_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def l2norm(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return (v / n).astype(np.float32) if n > 0 else v.astype(np.float32)


def cosine(a: np.ndarray | None, b: np.ndarray | None) -> float | None:
    if a is None or b is None or a.shape != b.shape:
        return None
    return float(np.clip(np.dot(a, b), -1.0, 1.0))


class VehicleReIdentifier(ABC):
    name: str = "abstract"
    dim: int = 0

    @abstractmethod
    def embed(self, crops: list[np.ndarray]) -> list[np.ndarray]: ...


class MobileNetReIdentifier(VehicleReIdentifier):
    """Global-average-pooled MobileNetV2 features (1280-d) plus a colour histogram.

    ImageNet features capture shape/structure; the appended HSV histogram adds the
    colour layout that generic CNN features under-weight. Swap in a dedicated
    vehicle Re-ID network (e.g. VeRi-trained OSNet) via configs/models.yaml.
    """

    name = "mobilenetv2_onnx"

    def __init__(self, path: Path, input_size: int, device: str, threads: int) -> None:
        self.session = get_session(path, device, threads)
        self.input_name = self.session.get_inputs()[0].name
        self.size = input_size
        self.dim = 1280 + ColorHistogramReIdentifier.dim

    def embed(self, crops: list[np.ndarray]) -> list[np.ndarray]:
        if not crops:
            return []
        batch = []
        for c in crops:
            x = cv2.resize(c, (self.size, self.size), interpolation=cv2.INTER_AREA)
            x = (cv2.cvtColor(x, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0 - _MEAN) / _STD
            batch.append(x.transpose(2, 0, 1))
        feats = self.session.run(None, {self.input_name: np.stack(batch).astype(np.float32)})[0].reshape(len(crops), -1)
        hists = ColorHistogramReIdentifier().embed(crops)
        return [l2norm(np.concatenate([l2norm(f) * 0.85, h * 0.53])) for f, h in zip(feats, hists)]


class ColorHistogramReIdentifier(VehicleReIdentifier):
    """Model-free fallback: spatial HSV histograms (top/bottom halves)."""

    name = "color_histogram"
    dim = 2 * 8 * 4 * 4

    def embed(self, crops: list[np.ndarray]) -> list[np.ndarray]:
        out = []
        for c in crops:
            hsv = cv2.cvtColor(cv2.resize(c, (64, 64)), cv2.COLOR_BGR2HSV)
            parts = []
            for half in (hsv[:32], hsv[32:]):
                h = cv2.calcHist([half], [0, 1, 2], None, [8, 4, 4], [0, 180, 0, 256, 0, 256]).reshape(-1)
                parts.append(l2norm(np.sqrt(h)))
            out.append(l2norm(np.concatenate(parts)))
        return out
