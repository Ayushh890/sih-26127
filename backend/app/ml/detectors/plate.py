"""Licence-plate detector implementations (operate on vehicle crops)."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.ml.detectors.base import Detection, PlateDetector, letterbox
from app.ml.runtime import get_session


class YoloV9PlateDetector(PlateDetector):
    """YOLOv9-t licence-plate detector with built-in NMS (open-image-models, MIT)."""

    name = "yolov9_onnx"

    def __init__(self, path: Path, input_size: int, device: str, threads: int) -> None:
        self.session = get_session(path, device, threads)
        self.input_name = self.session.get_inputs()[0].name
        self.size = input_size

    def detect(self, crop: np.ndarray, conf_threshold: float) -> list[Detection]:
        img, r, (dx, dy) = letterbox(crop, self.size, center=True)
        x = (img.transpose(2, 0, 1)[::-1] / 255.0).astype(np.float32)[None]
        out = self.session.run(None, {self.input_name: x})[0]
        h, w = crop.shape[:2]
        dets = []
        for row in out:
            score = float(row[6])
            if score < conf_threshold:
                continue
            x1, y1, x2, y2 = (row[1] - dx) / r, (row[2] - dy) / r, (row[3] - dx) / r, (row[4] - dy) / r
            dets.append(Detection(float(x1), float(y1), float(x2), float(y2), score, "plate").clip(w, h))
        return sorted(dets, key=lambda d: -d.confidence)


class ContourPlateDetector(PlateDetector):
    """Classical fallback: bright, high-contrast rectangles with plate-like aspect ratio."""

    name = "contour"

    def detect(self, crop: np.ndarray, conf_threshold: float) -> list[Detection]:
        h, w = crop.shape[:2]
        gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
        blur = cv2.bilateralFilter(gray, 7, 40, 40)
        edges = cv2.Canny(blur, 60, 180)
        edges = cv2.dilate(edges, None, iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        dets: list[Detection] = []
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            if bh == 0 or bw < w * 0.15 or bw > w * 0.8:
                continue
            aspect = bw / bh
            if not 2.5 <= aspect <= 6.0 or y < h * 0.3:
                continue
            roi = gray[y : y + bh, x : x + bw]
            contrast = float(roi.std()) / 64.0
            conf = float(min(0.8, 0.25 + contrast * 0.4))
            if conf >= conf_threshold:
                dets.append(Detection(x, y, x + bw, y + bh, conf, "plate"))
        dets.sort(key=lambda d: -d.confidence)
        return dets[:2]
