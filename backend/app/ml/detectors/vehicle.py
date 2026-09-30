"""Vehicle detector implementations."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from app.ml.detectors.base import Detection, VehicleDetector, letterbox, nms
from app.ml.runtime import get_session


class YoloxOnnxDetector(VehicleDetector):
    """YOLOX (Megvii, Apache-2.0) exported to ONNX with raw head outputs."""

    name = "yolox_onnx"

    def __init__(self, path: Path, input_size: int, classes: dict[int, str], nms_iou: float, device: str, threads: int) -> None:
        self.session = get_session(path, device, threads)
        self.input_name = self.session.get_inputs()[0].name
        self.size = input_size
        self.classes = {int(k): v for k, v in classes.items()}
        self.class_ids = np.array(sorted(self.classes))
        self.nms_iou = nms_iou
        grids, strides = [], []
        for st in (8, 16, 32):
            g = input_size // st
            yv, xv = np.meshgrid(np.arange(g), np.arange(g), indexing="ij")
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((g * g, 1), st))
        self._grids = np.concatenate(grids).astype(np.float32)
        self._strides = np.concatenate(strides).astype(np.float32)

    def detect(self, frame: np.ndarray, conf_threshold: float) -> list[Detection]:
        img, r, _ = letterbox(frame, self.size, center=False)
        x = img.transpose(2, 0, 1)[None].astype(np.float32)
        out = self.session.run(None, {self.input_name: x})[0][0]
        xy = (out[:, :2] + self._grids) * self._strides
        wh = np.exp(out[:, 2:4]) * self._strides
        cls_scores = out[:, 4:5] * out[:, 5:][:, self.class_ids]
        best = cls_scores.argmax(1)
        conf = cls_scores[np.arange(len(best)), best]
        keep = conf >= conf_threshold
        dets: list[Detection] = []
        h, w = frame.shape[:2]
        for (cx, cy), (bw, bh), c, k in zip(xy[keep], wh[keep], conf[keep], best[keep]):
            d = Detection((cx - bw / 2) / r, (cy - bh / 2) / r, (cx + bw / 2) / r, (cy + bh / 2) / r, float(c), self.classes[int(self.class_ids[k])])
            dets.append(d.clip(w, h))
        return nms(dets, self.nms_iou)  # class-agnostic: one box per physical vehicle


class MotionDetector(VehicleDetector):
    """Classical background-subtraction detector (no model required).

    Used as a fallback when no detection model is installed. It is stateful, so
    the registry creates one instance per camera. Vehicle class is estimated
    from blob size relative to the frame and is reported with low confidence.
    """

    name = "motion"

    def __init__(self, min_area_ratio: float = 0.004) -> None:
        self.bg = cv2.createBackgroundSubtractorMOG2(history=300, varThreshold=32, detectShadows=True)
        self.min_area_ratio = min_area_ratio
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))

    def detect(self, frame: np.ndarray, conf_threshold: float) -> list[Detection]:
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (w // 2, h // 2))
        mask = self.bg.apply(small)
        mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)[1]  # drop shadows (127)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, self.kernel, iterations=3)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        dets: list[Detection] = []
        frame_area = (w // 2) * (h // 2)
        for c in contours:
            x, y, bw, bh = cv2.boundingRect(c)
            ratio = bw * bh / frame_area
            if ratio < self.min_area_ratio:
                continue
            fill = cv2.contourArea(c) / max(1, bw * bh)
            conf = float(min(0.9, 0.3 + fill * 0.5))
            if conf < min(conf_threshold, 0.5):
                continue
            label = "motorcycle" if bw < bh * 0.6 and ratio < 0.02 else ("bus" if ratio > 0.12 else "car")
            dets.append(Detection(x * 2.0, y * 2.0, (x + bw) * 2.0, (y + bh) * 2.0, conf, label))
        return dets
