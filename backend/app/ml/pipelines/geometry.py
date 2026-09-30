"""Camera geometry: monocular speed, direction and lane estimation.

Speed uses a pinhole model: a vehicle of real width W (class prior, metres)
imaged ``w`` pixels wide is at depth ``Z = f * W / w``; lateral offset is
``X = (cx - c0) * Z / f``. Speed is the least-squares slope of the (X, Z)
trajectory over time. ``focal_px`` (default ≈ frame width, ~53° HFOV) should
be calibrated per camera for accurate speeds; a full ground-plane
``homography`` can be supplied instead. All speeds are reported as estimates.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

CLASS_WIDTH_M = {"car": 1.75, "motorcycle": 0.8, "bus": 2.55, "truck": 2.5, "van": 1.9, "auto_rickshaw": 1.4}
COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]


def compass_to_deg(c: str) -> float:
    return COMPASS.index(c) * 45.0 if c in COMPASS else 0.0


def deg_to_compass(d: float) -> str:
    return COMPASS[int(((d % 360) + 22.5) // 45) % 8]


def opposite(c: str) -> str:
    return deg_to_compass(compass_to_deg(c) + 180)


@dataclass
class CameraGeometry:
    frame_width: int
    frame_height: int
    direction: str = "N"  # compass heading of the monitored traffic flow
    lane_count: int = 2
    focal_px: float | None = None
    flow_toward_camera: bool = True  # ANPR cameras usually face oncoming traffic
    one_way: bool = False
    vanishing_point: tuple[float, float] = (0.5, 0.1)  # fractions of frame
    lane_boundaries: list[float] | None = None  # x-fractions at the bottom row, len = lanes + 1
    homography: list[list[float]] | None = None  # image(px) -> ground(m)
    stationary_kmh: float = 3.0

    @classmethod
    def from_camera(cls, cam: dict, frame_width: int, frame_height: int) -> "CameraGeometry":
        cal = cam.get("calibration") or {}
        scale = frame_width / float(cal.get("reference_width", frame_width))
        focal = cal.get("focal_px")
        return cls(
            frame_width=frame_width,
            frame_height=frame_height,
            direction=cam.get("direction", "N"),
            lane_count=int(cam.get("lane_count", 2) or 1),
            focal_px=float(focal) * scale if focal else None,
            flow_toward_camera=bool(cal.get("flow_toward_camera", True)),
            one_way=bool(cal.get("one_way", False)),
            vanishing_point=tuple(cal.get("vanishing_point", (0.5, 0.1))),  # type: ignore[arg-type]
            lane_boundaries=cal.get("lane_boundaries"),
            homography=cal.get("homography"),
        )

    @property
    def f(self) -> float:
        return self.focal_px or float(self.frame_width)

    def ground_xy(self, cx: float, cy_bottom: float, box_w: float, label: str) -> tuple[float, float]:
        if self.homography:
            H = np.array(self.homography, dtype=np.float64)
            p = H @ np.array([cx, cy_bottom, 1.0])
            return float(p[0] / p[2]), float(p[1] / p[2])
        z = self.f * CLASS_WIDTH_M.get(label, 1.75) / max(box_w, 1.0)
        x = (cx - self.frame_width / 2) * z / self.f
        return x, z

    def lane_of(self, cx: float, cy: float) -> int | None:
        if self.lane_count < 1:
            return None
        vx, vy = self.vanishing_point[0] * self.frame_width, self.vanishing_point[1] * self.frame_height
        h = self.frame_height
        if cy <= vy + 1:
            return None
        xb = vx + (cx - vx) * (h - vy) / (cy - vy)  # project to bottom row along the lane lines
        bounds = self.lane_boundaries or [i / self.lane_count for i in range(self.lane_count + 1)]
        frac = xb / self.frame_width
        for i in range(len(bounds) - 1):
            if bounds[i] <= frac < bounds[i + 1]:
                return i + 1
        return 1 if frac < bounds[0] else len(bounds) - 1


@dataclass
class MotionEstimate:
    speed_kmh: float | None
    motion: str  # with_flow | against_flow | stationary | unknown
    direction: str | None
    heading_deg: float | None


def estimate_motion(geo: CameraGeometry, samples: list[tuple[float, float, float, float, str]]) -> MotionEstimate:
    """samples: (t, cx, bottom_y, box_w, label) for frames where the box is fully visible."""
    if len(samples) < 3:
        return MotionEstimate(None, "unknown", None, None)
    t = np.array([s[0] for s in samples])
    if t[-1] - t[0] < 0.4:
        return MotionEstimate(None, "unknown", None, None)
    pts = np.array([geo.ground_xy(s[1], s[2], s[3], s[4]) for s in samples])
    tc = t - t.mean()
    denom = float((tc**2).sum()) or 1.0
    vx = float((tc * (pts[:, 0] - pts[:, 0].mean())).sum() / denom)
    vz = float((tc * (pts[:, 1] - pts[:, 1].mean())).sum() / denom)
    speed = float(np.hypot(vx, vz) * 3.6)
    if speed < geo.stationary_kmh:
        return MotionEstimate(round(speed, 1), "stationary", None, None)
    approaching = vz < 0
    with_flow = approaching == geo.flow_toward_camera
    direction = geo.direction if with_flow else opposite(geo.direction)
    return MotionEstimate(round(speed, 1), "with_flow" if with_flow else "against_flow", direction, compass_to_deg(direction))
