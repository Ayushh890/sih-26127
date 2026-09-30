"""Deterministic synthetic traffic schedule for the demo network.

Time is absolute (Unix seconds) and divided into cycles of ``cycle_seconds``.
Journeys for cycle ``k`` are generated from ``seed + k`` so every process (API,
workers, video exporter) derives the *same* traffic from the wall clock
without coordination. Background vehicles get fresh plates each cycle; the
scripted scenario vehicles (cloned plate, wrong-way, obscured plate, circling
vehicle, tracked vehicle) recur every cycle so the demo is repeatable.

The schedule only decides *what a camera would see*. Nothing here is written
to the database: the pixels are rendered, then detected, tracked and read by
the real pipeline exactly like a live RTSP feed.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from app.demo.network import camera_edges, load_network

# bus/truck sprites exist but the flat renderer does not produce COCO-recognisable
# heavy vehicles, so the demo mix is limited to cars (see docs/demo-script.md)
KINDS = [("sedan", 0.45), ("hatch", 0.32), ("suv", 0.23)]
KIND_LABEL = {"sedan": "car", "hatch": "car", "suv": "car", "bus": "bus", "truck": "truck"}
KIND_WIDTH_M = {"sedan": 1.75, "hatch": 1.70, "suv": 1.85, "bus": 2.55, "truck": 2.5}
# BGR
COLORS = [
    ((236, 236, 236), 0.24), ((190, 190, 192), 0.17), ((118, 118, 122), 0.12), ((40, 40, 185), 0.09),
    ((140, 70, 30), 0.08), ((40, 30, 110), 0.06), ((70, 50, 45), 0.05), ((170, 200, 215), 0.06),
    ((30, 200, 235), 0.04), ((60, 110, 40), 0.03), ((20, 110, 230), 0.03), ((72, 72, 76), 0.03),
]
UP_DISTRICTS = ["32", "32", "32", "32", "32", "14", "78", "65", "41", "33"]
OTHER_STATES = [("DL", ["01", "03", "08", "12"]), ("HR", ["26", "55"]), ("MH", ["12", "02"]), ("BR", ["01", "06"]), ("UK", ["07"]), ("MP", ["04"])]
SERIES_LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"


@dataclass
class Pass:
    camera_id: str
    t: float  # absolute time the vehicle crosses the reference depth
    speed_kmh: float
    lane: int  # 1..lanes
    toward: bool = True  # False = moving away from the camera (against the flow on a one-way)
    stop_and_go: bool = False
    t_enter: float = 0.0
    t_exit: float = 0.0


@dataclass
class Journey:
    vid: str
    plate: str | None
    kind: str
    color: tuple[int, int, int]
    roof: tuple[int, int, int] | None = None
    sticker: bool = False
    variant: int = 0
    obscured: bool = False
    scripted: str | None = None
    passes: list[Pass] = field(default_factory=list)

    @property
    def label(self) -> str:
        return KIND_LABEL[self.kind]

    @property
    def width_m(self) -> float:
        return KIND_WIDTH_M[self.kind]


def _pick(rng: random.Random, weighted: list[tuple[Any, float]]) -> Any:
    x = rng.random() * sum(w for _, w in weighted)
    for v, w in weighted:
        x -= w
        if x <= 0:
            return v
    return weighted[-1][0]


def random_plate(rng: random.Random) -> str:
    r = rng.random()
    num = f"{rng.randint(1, 9999):04d}"
    if r < 0.03:
        return f"{rng.randint(21, 25)}BH{num}{rng.choice(SERIES_LETTERS)}{rng.choice(SERIES_LETTERS)}"
    if r < 0.82:
        state, district = "UP", rng.choice(UP_DISTRICTS)
    else:
        state, ds = rng.choice(OTHER_STATES)
        district = rng.choice(ds)
    series = rng.choice(SERIES_LETTERS) + rng.choice(SERIES_LETTERS) if rng.random() < 0.9 else rng.choice(SERIES_LETTERS)
    return f"{state}{district}{series}{num}"


class Scenario:
    def __init__(self, net: dict[str, Any], seed: int = 2026) -> None:
        self.net = net
        self.seed = seed
        self.L = float(net.get("cycle_seconds", 600))
        g = net["geometry"]
        self.z_far, self.z_exit, self.z_ref = g["z_far_m"], g["z_exit_m"], g["z_ref_m"]
        self.lanes = {c["id"]: int(c.get("lane_count", 2)) for c in net["cameras"]}
        self.one_way = {c["id"]: bool(c.get("one_way")) for c in net["cameras"]}
        self.edges = {(e["from_camera_id"], e["to_camera_id"]): e for e in camera_edges(net) if e["allowed"]}
        self.cong = net.get("scenario", {}).get("congestion")

    # ------------------------------------------------------------------ helpers
    def _congested(self, camera_id: str, t: float) -> bool:
        c = self.cong
        return bool(c) and c["camera_id"] == camera_id and c["start"] <= (t % self.L) < c["end"]

    def _pass(self, rng: random.Random, camera_id: str, t: float, base_kmh: float, lane: int | None = None, toward: bool = True) -> Pass:
        if self._congested(camera_id, t):
            lo, hi = self.cong["speed_kmh"]
            return Pass(camera_id, t, rng.uniform(lo, hi), lane or rng.randint(1, self.lanes[camera_id]), toward, stop_and_go=True)
        return Pass(camera_id, t, base_kmh * rng.uniform(0.85, 1.1), lane or rng.randint(1, self.lanes[camera_id]), toward)

    def _route_passes(self, rng: random.Random, cams: list[str], t0: float, base_kmh: float) -> list[Pass]:
        passes = [self._pass(rng, cams[0], t0, base_kmh)]
        t = t0
        lo, hi = self.net.get("signal_delay_s", [0, 20])
        for a, b in zip(cams, cams[1:]):
            e = self.edges[(a, b)]
            seg_kmh = base_kmh * rng.uniform(0.85, 1.12)
            t += e["distance_m"] / (seg_kmh / 3.6) + rng.uniform(lo, hi)
            if self._congested(b, t):
                # queueing delay on the approach; vehicles still discharge before the queue clears
                extra = e["distance_m"] / (seg_kmh / 3.6) * (self.cong["upstream_delay_factor"] - 1)
                room = self.cong["end"] + 30 - (t % self.L)
                t += min(extra, max(0.0, room)) * rng.uniform(0.6, 1.0)
            passes.append(self._pass(rng, b, t, base_kmh))
        return passes

    def _appearance(self, rng: random.Random) -> dict[str, Any]:
        kind = _pick(rng, KINDS)
        color = _pick(rng, COLORS)
        roof = _pick(rng, COLORS) if rng.random() < 0.08 else None
        return {"kind": kind, "color": color, "roof": roof, "sticker": rng.random() < 0.15, "variant": rng.randint(0, 5)}

    # ------------------------------------------------------------------ generation
    @lru_cache(maxsize=16)
    def cycle(self, k: int) -> tuple[Journey, ...]:
        rng = random.Random(self.seed * 100003 + k)
        base = k * self.L
        out: list[Journey] = []
        n = 0
        lo_kmh, hi_kmh = self.net.get("speed_kmh", [25, 45])
        obscured_ratio = float(self.net.get("obscured_plate_ratio", 0.05))

        def new(plate: str | None, **app: Any) -> Journey:
            nonlocal n
            n += 1
            return Journey(vid=f"C{k}-V{n:04d}", plate=plate, **app)

        for route in self.net["routes"]:
            t = rng.expovariate(route["rate_per_min"] / 60.0)
            while t < self.L:
                obscured = rng.random() < obscured_ratio
                j = new(None if obscured else random_plate(rng), obscured=obscured, **self._appearance(rng))
                j.passes = self._route_passes(rng, route["cameras"], base + t, rng.uniform(lo_kmh, hi_kmh))
                out.append(j)
                t += rng.expovariate(route["rate_per_min"] / 60.0)
        # local traffic seen by a single camera
        for cam in self.lanes:
            rate = float(self.net.get("local_rate_per_min", 1.0))
            if self.cong and cam == self.cong["camera_id"]:
                rate_extra = float(self.cong.get("extra_rate_per_min", 0))
            else:
                rate_extra = 0.0
            t = rng.expovariate(rate / 60.0)
            while t < self.L:
                j = new(random_plate(rng), **self._appearance(rng))
                j.passes = [self._pass(rng, cam, base + t, rng.uniform(lo_kmh, hi_kmh))]
                out.append(j)
                t += rng.expovariate(rate / 60.0)
            if rate_extra:
                t = self.cong["start"] + rng.expovariate(rate_extra / 60.0)
                while t < self.cong["end"]:
                    j = new(random_plate(rng), **self._appearance(rng))
                    j.passes = [self._pass(rng, cam, base + t, 30)]
                    out.append(j)
                    t += rng.expovariate(rate_extra / 60.0)
        out.extend(self._scripted(k, rng))
        self._resolve_conflicts(out)
        return tuple(out)

    def _scripted(self, k: int, rng: random.Random) -> list[Journey]:
        sc = self.net.get("scenario", {})
        base = k * self.L
        out: list[Journey] = []
        routes = {r["id"]: r["cameras"] for r in self.net["routes"]}
        if "tracked" in sc:
            s = sc["tracked"]
            j = Journey(f"C{k}-TRACKED", s["plate"], s["kind"], tuple(s["color"]), scripted="tracked", variant=2)
            j.passes = self._route_passes(random.Random(k * 7 + 1), routes[s["route"]], base + s["t"], 34)
            out.append(j)
        if "obscured" in sc:
            s = sc["obscured"]
            j = Journey(f"C{k}-OBSCURED", None, s["kind"], tuple(s["color"]), roof=tuple(s["roof"]) if s.get("roof") else None, sticker=bool(s.get("sticker")), obscured=True, scripted="obscured_plate", variant=4)
            j.passes = self._route_passes(random.Random(k * 7 + 2), routes[s["route"]], base + s["t"], 36)
            out.append(j)
        if "cloned_plate" in sc:
            s = sc["cloned_plate"]
            a, b = s["first"], s["second"]
            j1 = Journey(f"C{k}-CLONE-A", s["plate"], a["kind"], tuple(a["color"]), scripted="cloned_plate", variant=1)
            j1.passes = [self._pass(random.Random(k * 7 + 3), a["camera_id"], base + a["t"], 38)]
            j2 = Journey(f"C{k}-CLONE-B", s["plate"], b["kind"], tuple(b["color"]), scripted="cloned_plate", variant=3)
            j2.passes = [self._pass(random.Random(k * 7 + 4), b["camera_id"], base + b["t"], 40)]
            out += [j1, j2]
        if "wrong_way" in sc:
            s = sc["wrong_way"]
            j = Journey(f"C{k}-WRONGWAY", s["plate"], s["kind"], tuple(s["color"]), scripted="wrong_way", variant=5)
            j.passes = [self._pass(random.Random(k * 7 + 5), s["camera_id"], base + s["t"], 28, lane=1, toward=False)]
            out.append(j)
        if "circling" in sc:
            s = sc["circling"]
            j = Journey(f"C{k}-CIRCLING", s["plate"], s["kind"], tuple(s["color"]), scripted="circling", variant=0, sticker=True)
            r = random.Random(k * 7 + 6)
            j.passes = [self._pass(r, s["camera_id"], base + t, 26) for t in s["times"]]
            out.append(j)
        return out

    def _resolve_conflicts(self, journeys: list[Journey]) -> None:
        """Compute visibility windows and keep a safe headway per (camera, lane)."""
        by_lane: dict[tuple[str, int], list[Pass]] = {}
        for j in journeys:
            for p in j.passes:
                self._window(p)
                by_lane.setdefault((p.camera_id, p.lane), []).append(p)
        for (cam, lane), ps in by_lane.items():
            ps.sort(key=lambda p: p.t)
            lanes = self.lanes[cam]
            for prev, cur in zip(ps, ps[1:]):
                v = min(prev.speed_kmh, cur.speed_kmh) / 3.6
                need = 9.0 / max(v, 0.8)  # ~9 m bumper-to-bumper spacing at the slower speed
                if cur.t - prev.t < need and lanes > 1:
                    cur.lane = lane % lanes + 1  # move to the neighbouring lane
                    self._window(cur)

    def _window(self, p: Pass) -> None:
        v = max(p.speed_kmh, 1.0) / 3.6
        if p.toward:
            p.t_enter = p.t - (self.z_far - self.z_ref) / v
            p.t_exit = p.t + (self.z_ref - self.z_exit) / v
        else:
            p.t_enter = p.t - (self.z_ref - self.z_exit) / v
            p.t_exit = p.t + (self.z_far - self.z_ref) / v

    # ------------------------------------------------------------------ queries
    def depth_at(self, p: Pass, t: float) -> float:
        v = max(p.speed_kmh, 1.0) / 3.6
        dt = t - p.t
        if p.stop_and_go:
            w = 2 * math.pi / 24.0  # 24 s stop-and-go wave shared by the whole queue
            dt = dt + (math.sin(w * t) - math.sin(w * p.t)) / w
        return self.z_ref - v * dt if p.toward else self.z_ref + v * dt

    def visible(self, camera_id: str, t: float) -> list[tuple[Journey, Pass, float]]:
        k = int(t // self.L)
        out = []
        for kk in range(k - 3, k + 1):
            if kk < 0:
                continue
            for j in self.cycle(kk):
                for p in j.passes:
                    if p.camera_id == camera_id and p.t_enter - 30 <= t <= p.t_exit + 30:
                        z = self.depth_at(p, t)
                        if self.z_exit <= z <= self.z_far:
                            out.append((j, p, z))
        out.sort(key=lambda x: -x[2])
        return out

    def ground_truth(self, t_from: float, t_to: float) -> list[dict[str, Any]]:
        """Scheduled passes in a time range — used by tests to measure accuracy, never by the pipeline."""
        rows = []
        for kk in range(max(0, int(t_from // self.L) - 3), int(t_to // self.L) + 1):
            for j in self.cycle(kk):
                for p in j.passes:
                    if t_from <= p.t < t_to:
                        rows.append({"vid": j.vid, "plate": j.plate, "camera_id": p.camera_id, "t": p.t, "speed_kmh": p.speed_kmh, "scripted": j.scripted, "toward": p.toward})
        return sorted(rows, key=lambda r: r["t"])


_scenarios: dict[int, Scenario] = {}


def get_scenario(seed: int = 2026) -> Scenario:
    if seed not in _scenarios:
        _scenarios[seed] = Scenario(load_network(), seed)
    return _scenarios[seed]
