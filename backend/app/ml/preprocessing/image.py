"""Image preprocessing: plate rectification/enhancement, frame quality, vehicle colour."""
from __future__ import annotations

import cv2
import numpy as np

_CLAHE = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(4, 8))


def expand_box(x1: float, y1: float, x2: float, y2: float, w: int, h: int, px: float = 0.08, py: float = 0.15) -> tuple[int, int, int, int]:
    bw, bh = x2 - x1, y2 - y1
    return (
        int(max(0, x1 - bw * px)),
        int(max(0, y1 - bh * py)),
        int(min(w, x2 + bw * px)),
        int(min(h, y2 + bh * py)),
    )


def _order_quad(pts: np.ndarray) -> np.ndarray:
    s = pts.sum(1)
    d = np.diff(pts, axis=1).reshape(-1)
    return np.array([pts[np.argmin(s)], pts[np.argmin(d)], pts[np.argmax(s)], pts[np.argmax(d)]], dtype=np.float32)


def rectify_plate(plate: np.ndarray) -> np.ndarray:
    """Perspective-correct a plate crop.

    Finds the dominant bright quadrilateral (the plate background) and warps it
    to an axis-aligned rectangle. If no convincing quadrilateral is found the
    crop is only deskewed by the dominant text angle, or returned unchanged.
    """
    h, w = plate.shape[:2]
    if h < 8 or w < 16:
        return plate
    gray = cv2.cvtColor(plate, cv2.COLOR_BGR2GRAY)
    _, th = cv2.threshold(cv2.GaussianBlur(gray, (3, 3), 0), 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(th, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if contours:
        c = max(contours, key=cv2.contourArea)
        if cv2.contourArea(c) > 0.45 * w * h:
            rect = cv2.minAreaRect(c)
            angle = rect[2] if rect[2] < 45 else rect[2] - 90
            if abs(angle) > 2.0:
                quad = _order_quad(cv2.boxPoints(rect))
                tw = int(max(np.linalg.norm(quad[0] - quad[1]), np.linalg.norm(quad[3] - quad[2])))
                thh = int(max(np.linalg.norm(quad[0] - quad[3]), np.linalg.norm(quad[1] - quad[2])))
                if tw > 10 and thh > 5:
                    dst = np.array([[0, 0], [tw - 1, 0], [tw - 1, thh - 1], [0, thh - 1]], dtype=np.float32)
                    m = cv2.getPerspectiveTransform(quad, dst)
                    return cv2.warpPerspective(plate, m, (tw, thh), borderMode=cv2.BORDER_REPLICATE)
    return plate


def enhance_plate(plate: np.ndarray) -> np.ndarray:
    """Contrast normalisation + mild sharpening; upsamples very small crops."""
    h, w = plate.shape[:2]
    if w < 96:
        scale = 96 / max(w, 1)
        plate = cv2.resize(plate, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_CUBIC)
    lab = cv2.cvtColor(plate, cv2.COLOR_BGR2LAB)
    lab[..., 0] = _CLAHE.apply(lab[..., 0])
    out = cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    blur = cv2.GaussianBlur(out, (0, 0), 1.0)
    return cv2.addWeighted(out, 1.5, blur, -0.5, 0)


def frame_quality(frame: np.ndarray) -> tuple[float, float]:
    """Return (sharpness, brightness). Sharpness = variance of Laplacian on a 320px thumbnail."""
    h, w = frame.shape[:2]
    scale = 320 / max(w, 1)
    small = cv2.resize(frame, (320, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var()), float(gray.mean())


def dominant_color(crop: np.ndarray) -> tuple[str, list[int]]:
    """Coarse vehicle body colour name + median BGR->RGB of the sampled region."""
    h, w = crop.shape[:2]
    if h < 8 or w < 8:
        return "unknown", [0, 0, 0]
    # sample the body: central band, avoiding windscreen (top) and tyres/road (bottom)
    region = crop[int(h * 0.42) : int(h * 0.68), int(w * 0.15) : int(w * 0.85)]
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.float32)
    bgr = region.reshape(-1, 3)
    med_h, med_s, med_v = np.median(hsv, axis=0)
    rgb = [int(v) for v in np.median(bgr, axis=0)[::-1]]
    if med_v < 55:
        return "black", rgb
    if med_s < 40:
        if med_v > 185:
            return "white", rgb
        if med_v > 120:
            return "silver", rgb
        return "grey", rgb
    hue = med_h * 2  # OpenCV hue is 0..180
    if hue < 12 or hue >= 340:
        name = "red"
    elif hue < 40:
        name = "orange" if med_v > 150 else "brown"
    elif hue < 70:
        name = "yellow"
    elif hue < 165:
        name = "green"
    elif hue < 255:
        name = "blue"
    elif hue < 300:
        name = "purple"
    else:
        name = "red"
    return name, rgb
