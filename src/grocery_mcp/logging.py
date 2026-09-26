from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "cookie",
    "cvv",
    "email",
    "id_token",
    "latitude",
    "longitude",
    "password",
    "phone",
    "profile_api_token",
    "refresh_token",
    "session_token",
    "sha1password",
    "token",
}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower() in _SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = record.exc_info[0].__name__ if record.exc_info[0] else "Error"
        return json.dumps(redact(payload), separators=(",", ":"))


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Both retailer protocols can require credentials in URL paths. The HTTP
    # client's INFO request line includes the full URL, so never emit it.
    for logger_name in ("httpx", "httpcore", "httpx2", "httpcore2"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
