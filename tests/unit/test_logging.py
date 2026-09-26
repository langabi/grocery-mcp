from __future__ import annotations

import logging

from grocery_mcp.logging import configure_logging


def test_http_client_request_urls_are_not_logged_at_info() -> None:
    logger_names = ("httpx", "httpcore", "httpx2", "httpcore2")
    previous = {name: logging.getLogger(name).level for name in logger_names}
    try:
        configure_logging("INFO")
        for name in logger_names:
            assert not logging.getLogger(name).isEnabledFor(logging.INFO)
    finally:
        for name, level in previous.items():
            logging.getLogger(name).setLevel(level)
