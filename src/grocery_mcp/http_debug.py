from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger("grocery_mcp.http_debug")

_SENSITIVE_PATHS = (re.compile(r"(/customer-profile/v\d+/)[^/]+"),)


def redacted_path(path: str) -> str:
    for pattern in _SENSITIVE_PATHS:
        path = pattern.sub(r"\1[REDACTED]", path)
    return path


def event_hooks(
    enabled: bool,
) -> dict[str, list[Callable[[httpx.Request | httpx.Response], Awaitable[None]]]]:
    if not enabled:
        return {}

    async def before(request: httpx.Request) -> None:
        request.extensions["grocery_started_at"] = time.monotonic()

    async def after(response: httpx.Response) -> None:
        started = response.request.extensions.get("grocery_started_at")
        latency_ms = round((time.monotonic() - started) * 1000) if started else None
        logger.info(
            "retailer_http method=%s host=%s path=%s status=%s latency_ms=%s",
            response.request.method,
            response.request.url.host,
            redacted_path(response.request.url.path),
            response.status_code,
            latency_ms,
        )

    return {"request": [before], "response": [after]}
