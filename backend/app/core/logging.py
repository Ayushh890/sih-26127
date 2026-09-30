"""Structured logging.

Every record carries ``timestamp``, ``service``, ``severity`` and ``message``;
``camera_id``, ``request_id``/``event_id`` and ``latency_ms`` are attached when
present either through ``extra=`` or through the context variables below.
Credentials embedded in URLs and ``token=`` query parameters (WebSocket and media URLs
carry the JWT that way) are scrubbed from every message, including uvicorn's own lines.
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import sys
from datetime import datetime, timezone

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
camera_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("camera_id", default=None)

_CRED_RE = re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://)([^:/@\s]+):([^@/\s]+)@")
_TOKEN_RE = re.compile(r"(token=)[^&\s\"']+")
_CONTEXT_FIELDS = ("camera_id", "request_id", "event_id", "latency_ms", "user", "status")


def scrub(text: str) -> str:
    """Remove ``user:password@`` credentials and ``token=`` values from any URL inside ``text``."""
    return _TOKEN_RE.sub(r"\1***", _CRED_RE.sub(r"\1***:***@", text))


class ScrubFilter(logging.Filter):
    """Scrubs records of loggers with their own handlers (uvicorn logs "WebSocket /ws/...?token=… [accepted]")."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = scrub(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "service": getattr(record, "service", record.name.split(".")[1] if record.name.count(".") else record.name),
            "severity": record.levelname,
            "logger": record.name,
            "message": scrub(record.getMessage()),
        }
        if (cid := getattr(record, "camera_id", None) or camera_id_var.get()) is not None:
            payload["camera_id"] = cid
        if (rid := getattr(record, "request_id", None) or request_id_var.get()) is not None:
            payload["request_id"] = rid
        for key in _CONTEXT_FIELDS[2:]:
            if (val := getattr(record, key, None)) is not None:
                payload[key] = val
        if record.exc_info:
            payload["exception"] = scrub(self.formatException(record.exc_info))
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        cid = getattr(record, "camera_id", None) or camera_id_var.get()
        prefix = f"[{cid}] " if cid else ""
        base = f"{self.formatTime(record, '%H:%M:%S')} {record.levelname:<7} {record.name}: {prefix}{scrub(record.getMessage())}"
        if record.exc_info:
            base += "\n" + scrub(self.formatException(record.exc_info))
        return base


def configure_logging(level: str = "INFO", json_logs: bool = True) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if json_logs else TextFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access", "httpx", "urllib3", "multipart"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        if not any(isinstance(f, ScrubFilter) for f in lg.filters):
            lg.addFilter(ScrubFilter())


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"nirnay.{name}")
