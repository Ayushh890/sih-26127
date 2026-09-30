"""Plate OCR engines."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from app.ml.runtime import get_session


@dataclass
class OCRResult:
    text: str
    confidence: float
    char_confidences: list[float]


class PlateOCR(ABC):
    name: str = "abstract"

    @abstractmethod
    def recognize(self, plates: list[np.ndarray]) -> list[OCRResult]:
        """Batch-recognise BGR plate crops."""


class CCTPlateOCR(PlateOCR):
    """Compact-Convolutional-Transformer OCR from fast-plate-ocr (MIT), fixed-slot output."""

    name = "cct_onnx"

    def __init__(self, path: Path, config_path: Path, device: str, threads: int) -> None:
        cfg = yaml.safe_load(config_path.read_text())
        self.alphabet: str = cfg["alphabet"]
        self.pad_char: str = cfg["pad_char"]
        self.h = int(cfg["img_height"])
        self.w = int(cfg["img_width"])
        self.rgb = cfg.get("image_color_mode", "rgb") == "rgb"
        self.session = get_session(path, device, threads)
        self.input_name = self.session.get_inputs()[0].name

    def _prep(self, img: np.ndarray) -> np.ndarray:
        x = cv2.resize(img, (self.w, self.h), interpolation=cv2.INTER_LINEAR)
        if self.rgb:
            return cv2.cvtColor(x, cv2.COLOR_BGR2RGB)
        return cv2.cvtColor(x, cv2.COLOR_BGR2GRAY)[..., None]

    def recognize(self, plates: list[np.ndarray]) -> list[OCRResult]:
        if not plates:
            return []
        batch = np.stack([self._prep(p) for p in plates]).astype(np.uint8)
        probs = self.session.run(None, {self.input_name: batch})[0]  # (N, slots, vocab), softmaxed
        results = []
        for p in probs:
            idx = p.argmax(-1)
            conf = p.max(-1)
            chars, confs = [], []
            for i, c in zip(idx, conf):
                ch = self.alphabet[int(i)]
                if ch == self.pad_char:
                    continue
                chars.append(ch)
                confs.append(float(c))
            text = "".join(chars)
            mean = float(np.mean(confs)) if confs else 0.0
            # the weakest character bounds how much we trust the whole read
            overall = mean * (min(confs) ** 0.25) if confs else 0.0
            results.append(OCRResult(text=text, confidence=overall, char_confidences=confs))
        return results
