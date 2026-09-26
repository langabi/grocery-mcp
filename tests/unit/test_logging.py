from __future__ import annotations

import argparse
import logging
from types import SimpleNamespace

import pytest

from grocery_mcp.cli import admin
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


@pytest.mark.asyncio
async def test_admin_configures_logging_before_building_application(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []
    settings = SimpleNamespace(log_level="INFO")

    monkeypatch.setattr(admin, "Settings", lambda: settings)
    monkeypatch.setattr(
        admin,
        "configure_logging",
        lambda level: calls.append(("configure_logging", level)),
        raising=False,
    )

    async def build_application(*args, **kwargs):
        calls.append(("build_application", args[0]))
        raise RuntimeError("stop after logging assertion")

    monkeypatch.setattr(admin, "build_application", build_application)

    with pytest.raises(RuntimeError, match="stop after logging assertion"):
        await admin._run(argparse.Namespace(command="health"))

    assert calls == [
        ("configure_logging", "INFO"),
        ("build_application", settings),
    ]
