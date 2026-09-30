"""Log scrubbing: no RTSP passwords and no JWTs in any log line, uvicorn's included."""
from __future__ import annotations

import logging

from app.core.logging import ScrubFilter, configure_logging, scrub

JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl"


def test_scrub_removes_credentials_and_tokens() -> None:
    assert scrub("rtsp://admin:s3cret@10.0.0.5/s") == "rtsp://***:***@10.0.0.5/s"
    assert scrub(f'"WebSocket /ws/cameras/LOCAL-01/ingest?token={JWT}" [accepted]') == '"WebSocket /ws/cameras/LOCAL-01/ingest?token=***" [accepted]'
    assert scrub(f"GET /api/cameras/C1/stream.mjpg?overlay=1&token={JWT}&fps=6") == "GET /api/cameras/C1/stream.mjpg?overlay=1&token=***&fps=6"
    assert JWT not in scrub(f"access_token={JWT}")


def test_uvicorn_websocket_line_is_scrubbed() -> None:
    configure_logging("INFO", json_logs=False)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    lg = logging.getLogger("uvicorn.error")
    assert any(isinstance(f, ScrubFilter) for f in lg.filters)
    h = Capture()
    lg.addHandler(h)
    try:  # the exact call uvicorn's websockets protocol makes
        lg.info('%s - "WebSocket %s" [accepted]', "127.0.0.1:5000", f"/ws/events?token={JWT}")
    finally:
        lg.removeHandler(h)
    assert records and JWT not in records[0].getMessage() and "token=***" in records[0].getMessage()
