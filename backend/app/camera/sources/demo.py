"""Synthetic demo camera.

Renders frames live from the deterministic demo traffic scenario
(:mod:`app.demo.scenario`) at the requested frame rate. The frames then go through
exactly the same worker → detector → tracker → plate detector → OCR → ingestion path
as frames from a physical camera; only the pixels are synthetic, and every record
produced from them is flagged ``is_demo``.
"""
from __future__ import annotations

import time

import numpy as np

from app.camera.sources.base import CameraSource, SourceError


class DemoSyntheticSource(CameraSource):
    source_type = "demo"

    def _open(self) -> None:
        from app.demo.renderer import SceneRenderer
        from app.demo.scenario import get_scenario

        cam = self.uri.replace("demo://", "").strip("/") or self.camera_id
        try:
            self.renderer = SceneRenderer(cam, get_scenario(int(self.options.get("seed", 2026))))
        except (KeyError, StopIteration):
            raise SourceError(f"demo scene {cam!r} is not defined in the demo network", permanent=True) from None
        self.native_fps = float(self.options.get("fps", self.renderer.scenario.net.get("fps", 10)))
        self._next_due = time.time()

    def _grab(self) -> np.ndarray | None:
        delay = self._next_due - time.time()
        if delay > 0:
            time.sleep(delay)
        self._next_due = max(self._next_due + 1.0 / self.native_fps, time.time() - 0.25)
        now = time.time()
        # scenario time = wall clock - epoch offset ("restart demo timeline" moves the offset)
        return self.renderer.render(now - float(self.options.get("epoch_offset_s", 0.0)), display_ts=now)
