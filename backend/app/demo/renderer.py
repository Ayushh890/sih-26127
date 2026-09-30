"""Synthetic street-camera renderer for the demo network.

Renders what a fixed ANPR camera would see given the deterministic schedule in
:mod:`app.demo.scenario`. Vehicles are drawn with the *same pinhole geometry*
the speed estimator inverts (width_px = f * W / Z, ground contact row =
horizon + f * h / Z), so measured speeds can be checked against ground truth.

Frames are ordinary BGR images; downstream code cannot tell them apart from a
decoded RTSP frame except via the camera's ``is_demo`` flag.
"""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any

import cv2
import numpy as np

from app.demo.scenario import Journey, Pass, Scenario
from app.ml.ocr.normalize import format_plate

SPRITE_CACHE_BYTES = 24 * 1024 * 1024  # per camera renderer


def _shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(int(max(0, min(255, v * f))) for v in c)  # type: ignore[return-value]


def plate_image(text: str | None, h: int, obscured: bool = False, rear: bool = False) -> np.ndarray:
    """White Indian HSRP-style plate with the registration in a DIN-like face."""
    h = max(8, h)
    w = int(h * 4.4)
    img = np.full((h, w, 3), 242, np.uint8)
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), (25, 25, 25), max(1, h // 14))
    strip = max(3, int(w * 0.07))
    cv2.rectangle(img, (2, 2), (strip, h - 3), (150, 60, 20), -1)  # blue IND strip
    if text and not obscured:
        s = format_plate(text)
        font = cv2.FONT_HERSHEY_DUPLEX
        thick = max(1, int(h / 12))
        (tw, th), _ = cv2.getTextSize(s, font, 1.0, 2)
        tmp = np.full((th + 12, tw + 8, 3), 242, np.uint8)
        cv2.putText(tmp, s, (4, th + 5), font, 1.0, (15, 15, 15), 2 + thick // 2, cv2.LINE_AA)
        tmp = cv2.resize(tmp, (w - strip - 6, h - 4), interpolation=cv2.INTER_AREA)
        img[2 : h - 2, strip + 3 : w - 3] = np.minimum(img[2 : h - 2, strip + 3 : w - 3], tmp)
    if obscured:
        rng = np.random.default_rng(len(text or "") + h)
        for _ in range(9):
            cx, cy = int(rng.integers(strip, w)), int(rng.integers(0, h))
            cv2.ellipse(img, (cx, cy), (int(w * 0.16), int(h * 0.55)), float(rng.integers(0, 180)), 0, 360, (40, 70, 95), -1)
    return img


def vehicle_sprite(j: Journey, W: int, rear: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """Front (or rear) view of a vehicle ``W`` pixels wide, with mask."""
    kind, color, v = j.kind, j.color, j.variant
    aspect = {"sedan": 0.98, "hatch": 1.02, "suv": 1.10, "bus": 1.35, "truck": 1.30}[kind]
    H = max(8, int(W * aspect))
    img = np.zeros((H, W, 3), np.uint8)
    P = lambda pts: np.array([[int(x * W), int(y * H)] for x, y in pts], np.int32)  # noqa: E731
    roof_c = j.roof or _shade(color, 1.12)
    glass = (58, 50, 44)
    if kind in ("bus", "truck"):
        cv2.rectangle(img, (int(0.06 * W), int(0.86 * H)), (int(0.22 * W), H - 1), (18, 18, 18), -1)
        cv2.rectangle(img, (int(0.78 * W), int(0.86 * H)), (int(0.94 * W), H - 1), (18, 18, 18), -1)
        cv2.fillPoly(img, [P([(0.02, 0.02), (0.98, 0.02), (0.99, 0.90), (0.01, 0.90)])], color)
        cv2.fillPoly(img, [P([(0.06, 0.10 if kind == "bus" else 0.30), (0.94, 0.10 if kind == "bus" else 0.30), (0.95, 0.50), (0.05, 0.50)])], glass)
        if kind == "truck":
            cv2.rectangle(img, (int(0.02 * W), int(0.02 * H)), (int(0.98 * W), int(0.26 * H)), _shade(roof_c, 0.9), -1)
        cv2.line(img, (int(0.5 * W), int(0.10 * H)), (int(0.5 * W), int(0.5 * H)), _shade(color, 0.7), max(1, W // 60))
        for x0 in (0.06, 0.80):
            cv2.rectangle(img, (int(x0 * W), int(0.58 * H)), (int((x0 + 0.14) * W), int(0.65 * H)), (40, 40, 200) if rear else (235, 240, 245), -1)
        cv2.rectangle(img, (int(0.30 * W), int(0.56 * H)), (int(0.70 * W), int(0.68 * H)), (35, 35, 35), -1)
        cv2.rectangle(img, (int(0.02 * W), int(0.84 * H)), (int(0.98 * W), int(0.90 * H)), (30, 30, 30), -1)
        plate_y = 0.71
    else:
        top = {"sedan": 0.03, "hatch": 0.0, "suv": 0.0}[kind]
        for x0 in (0.05, 0.80):
            cv2.rectangle(img, (int(x0 * W), int(0.80 * H)), (int((x0 + 0.15) * W), H - 1), (18, 18, 18), -1)
        cv2.fillPoly(img, [P([(0.02, 0.52), (0.98, 0.52), (0.99, 0.90), (0.01, 0.90)])], _shade(color, 0.86))
        hood_top = 0.40 if kind != "suv" else 0.42
        cv2.fillPoly(img, [P([(0.08, hood_top), (0.92, hood_top), (0.99, 0.55), (0.01, 0.55)])], _shade(color, 1.06))
        ws_top = 0.14 + top
        cv2.fillPoly(img, [P([(0.20, ws_top), (0.80, ws_top), (0.91, hood_top), (0.09, hood_top)])], glass)
        if not rear:
            cv2.fillPoly(img, [P([(0.30, ws_top + 0.02), (0.42, ws_top + 0.02), (0.30, hood_top - 0.02), (0.18, hood_top - 0.02)])], (96, 90, 82))
        roof_w = 0.24 if kind != "suv" else 0.18
        cv2.fillPoly(img, [P([(roof_w, top + 0.02), (1 - roof_w, top + 0.02), (0.80, ws_top), (0.20, ws_top)])], roof_c)
        if kind == "suv":
            cv2.rectangle(img, (int(0.22 * W), int(0.01 * H)), (int(0.78 * W), int(0.03 * H)), (40, 40, 40), -1)  # roof rails
        lw = max(2, W // 40)
        cv2.line(img, tuple(P([(0.20, ws_top)])[0]), tuple(P([(0.09, hood_top)])[0]), _shade(color, 0.9), lw)
        cv2.line(img, tuple(P([(0.80, ws_top)])[0]), tuple(P([(0.91, hood_top)])[0]), _shade(color, 0.9), lw)
        for pts in ([(0.0, 0.33), (0.08, 0.33), (0.08, 0.40), (0.0, 0.40)], [(0.92, 0.33), (1.0, 0.33), (1.0, 0.40), (0.92, 0.40)]):
            cv2.fillPoly(img, [P(pts)], _shade(color, 0.8))
        lamp = (40, 40, 210) if rear else (235, 240, 245)
        lamp_style = v % 3
        if lamp_style == 0:
            lamps = ([(0.05, 0.56), (0.27, 0.58), (0.25, 0.65), (0.04, 0.63)], [(0.95, 0.56), (0.73, 0.58), (0.75, 0.65), (0.96, 0.63)])
        elif lamp_style == 1:
            lamps = ([(0.04, 0.56), (0.22, 0.56), (0.22, 0.62), (0.04, 0.62)], [(0.78, 0.56), (0.96, 0.56), (0.96, 0.62), (0.78, 0.62)])
        else:
            lamps = ([(0.05, 0.57), (0.30, 0.56), (0.28, 0.61), (0.06, 0.64)], [(0.95, 0.57), (0.70, 0.56), (0.72, 0.61), (0.94, 0.64)])
        for pts in lamps:
            cv2.fillPoly(img, [P(pts)], lamp)
        if not rear:
            gw = 0.30 + 0.04 * (v % 2)
            cv2.rectangle(img, tuple(P([(0.5 - gw / 2, 0.57)])[0]), tuple(P([(0.5 + gw / 2, 0.66)])[0]), (28, 28, 28), -1)
            for i in range(1, 4):
                y = 0.57 + i * 0.0225
                cv2.line(img, tuple(P([(0.5 - gw / 2 + 0.01, y)])[0]), tuple(P([(0.5 + gw / 2 - 0.01, y)])[0]), (70, 70, 70), 1)
        cv2.rectangle(img, tuple(P([(0.03, 0.84)])[0]), tuple(P([(0.97, 0.90)])[0]), (35, 35, 35), -1)
        if v % 2 == 0 and not rear:
            for x in (0.10, 0.84):
                cv2.circle(img, tuple(P([(x, 0.80)])[0]), max(2, W // 40), (200, 200, 190), -1)
        plate_y = 0.69
    if j.sticker and not rear:
        cv2.rectangle(img, tuple(P([(0.66, 0.30 if kind not in ("bus", "truck") else 0.40)])[0]), tuple(P([(0.76, 0.37 if kind not in ("bus", "truck") else 0.47)])[0]), (30, 200, 240), -1)
    mask = (img.sum(axis=2) > 0).astype(np.uint8) * 255
    if W > 40:
        img = cv2.GaussianBlur(img, (3, 3), 0)
    ph = max(6, int(W * 0.13))
    plate = plate_image(j.plate, ph, obscured=j.obscured, rear=rear)
    if plate.shape[1] < W - 4:
        py, px = int(H * plate_y), (W - plate.shape[1]) // 2
        py = min(py, H - plate.shape[0] - 1)
        img[py : py + plate.shape[0], px : px + plate.shape[1]] = plate
        mask[py : py + plate.shape[0], px : px + plate.shape[1]] = 255
    return img, mask


class SceneRenderer:
    """Renders one demo camera. Thread-confined (one per camera source)."""

    def __init__(self, camera_id: str, scenario: Scenario) -> None:
        net = scenario.net
        self.camera_id = camera_id
        self.scenario = scenario
        self.cam = next(c for c in net["cameras"] if c["id"] == camera_id)
        self.W, self.H = net.get("frame_size", [960, 540])
        g = net["geometry"]
        self.f = float(g["focal_px"]) * self.W / 960.0
        self.cam_h = float(g["camera_height_m"])
        self.vx, self.vy = g["vanishing_point"][0] * self.W, g["vanishing_point"][1] * self.H
        self.lane_w = float(g["lane_width_m"])
        self.lanes = int(self.cam.get("lane_count", 2))
        self.background = self._background()
        self._sprites: OrderedDict[tuple, tuple[np.ndarray, np.ndarray]] = OrderedDict()
        self._sprite_bytes = 0

    # ------------------------------------------------------------------ geometry
    def project(self, X: float, Z: float) -> tuple[float, float]:
        return self.vx + X * self.f / Z, self.vy + self.f * self.cam_h / Z

    def lane_center_x(self, lane: int) -> float:
        return (lane - 0.5 - self.lanes / 2) * self.lane_w

    def calibration(self) -> dict[str, Any]:
        """Calibration block matching this renderer — seeded onto the demo camera record."""
        H, W = self.H, self.W
        per_m = (H - self.vy) / (self.cam_h * W)
        bounds = [round(0.5 + (i - self.lanes / 2) * self.lane_w * per_m, 4) for i in range(self.lanes + 1)]
        return {
            "reference_width": W, "focal_px": self.f, "flow_toward_camera": True, "one_way": bool(self.cam.get("one_way")),
            "vanishing_point": [self.vx / W, self.vy / H], "lane_boundaries": bounds, "camera_height_m": self.cam_h,
            "source": "synthetic renderer geometry",
        }

    # ------------------------------------------------------------------ scene
    def _background(self) -> np.ndarray:
        sc = self.cam.get("scene", {})
        rng = np.random.default_rng(sc.get("seed", 1))
        W, H, vy = self.W, self.H, int(self.vy)
        k = W / 960.0  # scene details are authored for 960 px and scaled
        img = np.zeros((H, W, 3), np.uint8)
        sky = np.array(sc.get("sky", [200, 190, 170]), np.float32)
        for y in range(vy + 1):
            img[y] = np.clip(sky * (0.92 + 0.12 * y / max(vy, 1)), 0, 255)
        ground = np.array(sc.get("ground", [95, 97, 99]), np.float32)
        img[vy:] = ground
        # sidewalks / verges outside the carriageway
        half = self.lanes / 2 * self.lane_w
        for side in (-1, 1):
            edge_near = self.project(side * (half + 0.3), 4.0)[0]
            far = self.project(side * (half + 0.3), 400.0)[0]
            outer = [(far, vy), (side * 4000 + W / 2, vy), (side * 4000 + W / 2, H), (edge_near, H)]
            cv2.fillPoly(img, [np.array(outer, np.int32)], (120, 132, 128) if side < 0 else (112, 126, 122))
        # buildings on the horizon
        for _ in range(int(sc.get("buildings", 6))):
            bw, bh = int(rng.integers(40, 140) * k), int(rng.integers(20, max(24, vy / k + 20)) * k)
            x = int(rng.integers(-20, W))
            col = tuple(int(c) for c in rng.integers(120, 220, 3))
            cv2.rectangle(img, (x, vy - bh), (x + bw, vy + 4), col, -1)
            step, win = int(11 * k), int(5 * k)
            for wy in range(vy - bh + int(6 * k), vy - int(4 * k), step):
                for wx in range(x + int(5 * k), x + bw - int(6 * k), step):
                    cv2.rectangle(img, (wx, wy), (wx + win, wy + win), _shade(col, 0.7), -1)
        for _ in range(int(sc.get("trees", 4))):
            x = int(rng.integers(0, W))
            if abs(x - W / 2) < 120 * k:
                continue
            r = int(rng.integers(14, 34) * k)
            cv2.rectangle(img, (x - int(2 * k), vy - int(4 * k)), (x + int(2 * k), vy + int(10 * k)), (40, 60, 70), -1)
            cv2.circle(img, (x, vy - r // 2), r, (50, int(rng.integers(100, 140)), 60), -1)
        # carriageway texture
        noise = rng.normal(0, 5, (H - vy, W, 1))
        img[vy:] = np.clip(img[vy:].astype(np.float32) + noise, 0, 255).astype(np.uint8)
        # lane markings: solid edges, dashed separators (dash pattern fixed in world metres)
        for i in range(self.lanes + 1):
            X = (i - self.lanes / 2) * self.lane_w
            solid = i in (0, self.lanes)
            if solid:
                a, b = self.project(X, 3.0), self.project(X, 600.0)
                cv2.line(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (210, 214, 214), max(2, int(3 * k)), cv2.LINE_AA)
            else:
                z = 3.0
                while z < 300:
                    a, b = self.project(X, z), self.project(X, z + 3.0)
                    cv2.line(img, (int(a[0]), int(a[1])), (int(b[0]), int(b[1])), (215, 218, 218), max(1, int(3 * k * 12 / z)), cv2.LINE_AA)
                    z += 9.0
        # zebra crossing near the camera
        for k in range(-int(half), int(half) + 1):
            p1, p2 = self.project(k - 0.35, 7.0), self.project(k + 0.35, 8.2)
            cv2.rectangle(img, (int(p1[0]), int(p2[1])), (int(p2[0]), int(p1[1])), (205, 208, 208), -1)
        return cv2.GaussianBlur(img, (3, 3), 0)

    # ------------------------------------------------------------------ vehicles
    def _sprite(self, j: Journey, w: int, rear: bool) -> tuple[np.ndarray, np.ndarray]:
        key = (j.vid, w, rear)
        s = self._sprites.get(key)
        if s is not None:
            self._sprites.move_to_end(key)
            return s
        s = vehicle_sprite(j, w, rear)
        self._sprites[key] = s
        self._sprite_bytes += s[0].nbytes + s[1].nbytes
        # LRU bounded by bytes: near-camera sprites are large and each size is used for a few frames only
        while self._sprite_bytes > SPRITE_CACHE_BYTES and len(self._sprites) > 1:
            _, (a, b) = self._sprites.popitem(last=False)
            self._sprite_bytes -= a.nbytes + b.nbytes
        return s

    def _draw_vehicle(self, frame: np.ndarray, j: Journey, p: Pass, Z: float) -> list[float] | None:
        X = self.lane_center_x(p.lane)
        w = int(round(self.f * j.width_m / Z))
        if w < 10:
            return None
        cx, bottom = self.project(X, Z)
        spr, mask = self._sprite(j, w, rear=not p.toward)
        sh, sw = spr.shape[:2]
        x0, y0 = int(round(cx - sw / 2)), int(round(bottom - sh))
        # soft shadow
        shw = int(sw * 0.58)
        sx0, sy0 = max(0, int(cx) - shw - 8), max(0, int(bottom) - int(sw * 0.12))
        sx1, sy1 = min(self.W, int(cx) + shw + 8), min(self.H, int(bottom) + int(sw * 0.12))
        if sx1 > sx0 and sy1 > sy0:
            roi = frame[sy0:sy1, sx0:sx1]
            m = np.zeros(roi.shape[:2], np.float32)
            cv2.ellipse(m, (int(cx) - sx0, int(bottom) - sy0 - 2), (shw, max(2, int(sw * 0.07))), 0, 0, 360, 1.0, -1)
            m = cv2.GaussianBlur(m, (0, 0), max(1.0, sw * 0.03))
            roi[:] = (roi * (1 - 0.5 * m[..., None])).astype(np.uint8)
        fx0, fy0, fx1, fy1 = max(0, x0), max(0, y0), min(self.W, x0 + sw), min(self.H, y0 + sh)
        if fx1 <= fx0 or fy1 <= fy0:
            return None
        sub = frame[fy0:fy1, fx0:fx1]
        s_spr = spr[fy0 - y0 : fy1 - y0, fx0 - x0 : fx1 - x0]
        s_mask = mask[fy0 - y0 : fy1 - y0, fx0 - x0 : fx1 - x0] > 0
        sub[s_mask] = s_spr[s_mask]
        return [float(x0), float(y0), float(x0 + sw), float(y0 + sh)]

    def render(self, t: float | None = None, osd: bool = True, display_ts: float | None = None) -> np.ndarray:
        """Render scenario time ``t``; ``display_ts`` is the wall-clock time shown in the OSD."""
        t = time.time() if t is None else t
        frame = self.background.copy()
        for j, p, Z in self.scenario.visible(self.camera_id, t):
            self._draw_vehicle(frame, j, p, Z)
        if osd:
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(display_ts if display_ts is not None else t))
            label = f"{self.camera_id}  {self.cam['name']}  {ts}  [SYNTHETIC]"
            k = self.W / 960.0
            cv2.rectangle(frame, (0, 0), (min(self.W, int((12 + 9 * len(label)) * k)), int(24 * k)), (0, 0, 0), -1)
            cv2.putText(frame, label, (int(6 * k), int(17 * k)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * k, (255, 255, 255), max(1, int(k)), cv2.LINE_AA)
        return frame

    def truth(self, t: float) -> list[dict[str, Any]]:
        """Ground-truth boxes for tests (never consumed by the pipeline)."""
        out = []
        for j, p, Z in self.scenario.visible(self.camera_id, t):
            X = self.lane_center_x(p.lane)
            w = self.f * j.width_m / Z
            cx, bottom = self.project(X, Z)
            out.append({"vid": j.vid, "plate": j.plate, "Z": Z, "speed_kmh": p.speed_kmh, "box": [cx - w / 2, bottom - w, cx + w / 2, bottom], "toward": p.toward})
        return out
