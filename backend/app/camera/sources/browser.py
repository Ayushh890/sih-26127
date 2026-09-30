"""Browser Local Camera source.

The operator's browser captures the laptop webcam with ``getUserMedia`` and uploads JPEG
frames over ``/ws/cameras/{id}/ingest`` (see :mod:`app.api.routes.local_camera`). The API
drops each upload into the camera's bounded input inbox on the event bus; this source
takes them from there. That works in both topologies: in-process when the workers are
embedded in the API, through Redis when they run in a separate worker container (which
has no access to the laptop's camera itself).

From here on the frames are ordinary ``(frame, ts)`` pairs: the same stream worker,
health monitor, detector, tracker, OCR and ingestion path as RTSP and file sources.
"""
from __future__ import annotations

import cv2
import numpy as np

from app.camera.sources.base import CameraSource, SourceError
from app.core.bus import get_bus

WAITING = "waiting for the browser to send frames (open Local Camera in the console and press Start)"


class BrowserPushSource(CameraSource):
    source_type = "browser"
    # there is no device to back off from: pick up a browser that starts streaming quickly
    max_retry_delay = 2.0

    def _open(self) -> None:
        self.bus = get_bus()
        self.native_fps = None  # set by the browser, varies with the tab's load
        got = self.bus.pop_input_frame(self.camera_id, timeout=float(self.options.get("open_timeout_s", 3.0)))
        frame = self._decode(got[0]) if got else None
        if frame is None:
            raise SourceError(self.last_error if got else WAITING)
        self.resolution = (int(frame.shape[1]), int(frame.shape[0]))
        self._first = frame

    def _grab(self) -> np.ndarray | None:
        first = getattr(self, "_first", None)
        if first is not None:
            self._first = None
            return first
        got = self.bus.pop_input_frame(self.camera_id, timeout=0.5)
        return self._decode(got[0]) if got else None

    def _decode(self, jpeg: bytes) -> np.ndarray | None:
        frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            self.stats.frames_dropped += 1
            self.last_error = "browser sent a frame that is not a decodable JPEG"
        return frame

    def _close(self) -> None:
        self._first = None
