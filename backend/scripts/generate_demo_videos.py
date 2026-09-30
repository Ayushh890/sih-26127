#!/usr/bin/env python3
"""Pre-render the synthetic demo scenario to MP4 files ("recorded" demo mode).

    python scripts/generate_demo_videos.py                 # all demo cameras, one full cycle
    python scripts/generate_demo_videos.py --cameras CAM-01,CAM-02 --seconds 120

Writes ``DATA_DIR/demo/videos/<CAMERA>.mp4`` plus ``<CAMERA>.truth.jsonl`` (per-second
ground truth: which scripted/background vehicle is where, for offline accuracy checks).
With ``DEMO_SOURCE=recorded`` the seeder registers the demo cameras as ordinary *video
file* sources pointing at these files (wall-clock synchronised, looping), so the demo
exercises the file-ingest path instead of rendering frames live — useful on small
machines, since rendering costs CPU. The pixels are identical to the live synthetic
renderer; every frame carries a ``[SYNTHETIC - RECORDED]`` banner and all resulting data is
flagged ``is_demo``.

The recording covers scenario cycle 0 (``cycle_seconds`` long). Background plates are
cycle-specific, so a replayed loop shows the same background vehicles every cycle;
scripted events (cloned plate, wrong way, congestion, ...) are identical in every cycle
and match ``GET /api/demo/timeline``.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cv2  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.demo.network import load_network  # noqa: E402
from app.demo.renderer import SceneRenderer  # noqa: E402
from app.demo.scenario import get_scenario  # noqa: E402


def banner(frame, text: str) -> None:
    h, w = frame.shape[:2]
    k = w / 960.0
    cv2.rectangle(frame, (0, 0), (min(w, int((12 + 9 * len(text)) * k)), int(24 * k)), (0, 0, 0), -1)
    cv2.putText(frame, text, (int(6 * k), int(17 * k)), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * k, (255, 255, 255), max(1, int(k)), cv2.LINE_AA)


def render_camera(cam: dict, seconds: float, fps: float, out_dir: Path) -> dict:
    sc = get_scenario()
    r = SceneRenderer(cam["id"], sc)
    path = out_dir / f"{cam['id']}.mp4"
    tmp = path.with_suffix(".part.mp4")
    writer = cv2.VideoWriter(str(tmp), cv2.VideoWriter_fourcc(*"mp4v"), fps, (r.W, r.H))
    if not writer.isOpened():
        raise RuntimeError("OpenCV could not open an MP4 writer (mp4v codec unavailable)")
    n = int(round(seconds * fps))
    label = f"{cam['id']}  {cam['name']}  [SYNTHETIC - RECORDED]"
    t0 = time.time()
    with (out_dir / f"{cam['id']}.truth.jsonl").open("w") as truth:
        for i in range(n):
            t = i / fps
            frame = r.render(t, osd=False)
            banner(frame, label)
            writer.write(frame)
            if i % int(fps) == 0:
                truth.write(json.dumps({"t": round(t, 2), "vehicles": [{**v, "box": [round(x, 1) for x in v["box"]]} for v in r.truth(t)]}) + "\n")
    writer.release()
    tmp.replace(path)
    return {"camera": cam["id"], "frames": n, "seconds": seconds, "file": str(path), "mb": round(path.stat().st_size / 1e6, 1), "render_s": round(time.time() - t0, 1)}


def main() -> int:
    net = load_network()
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cameras", default="", help="comma-separated camera ids (default: all demo cameras)")
    ap.add_argument("--seconds", type=float, default=float(net.get("cycle_seconds", 600)), help="length (default: one scenario cycle)")
    ap.add_argument("--fps", type=float, default=float(net.get("fps", 10)))
    ap.add_argument("--out", type=Path, default=get_settings().demo_video_dir)
    a = ap.parse_args()
    wanted = {c.strip() for c in a.cameras.split(",") if c.strip()}
    cams = [c for c in net["cameras"] if not wanted or c["id"] in wanted]
    unknown = wanted - {c["id"] for c in cams}
    if unknown:
        print(f"unknown demo cameras: {sorted(unknown)}", file=sys.stderr)
        return 2
    a.out.mkdir(parents=True, exist_ok=True)
    print(f"rendering {len(cams)} camera(s), {a.seconds:.0f}s at {a.fps:g} fps -> {a.out}")
    for c in cams:
        print("  ", render_camera(c, a.seconds, a.fps, a.out), flush=True)
    print("done. Start the platform with DEMO_SOURCE=recorded on a fresh database to use these files.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
