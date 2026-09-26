from __future__ import annotations

import os

import pytest

from grocery_mcp.app import build_application
from grocery_mcp.config import Settings
from grocery_mcp.domain.models import Retailer

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.getenv("RUN_LIVE_GROCERY_TESTS") != "1",
        reason="set RUN_LIVE_GROCERY_TESTS=1 to opt into retailer network calls",
    ),
]


async def test_live_anonymous_catalogue_searches(tmp_path) -> None:
    application = await build_application(
        Settings(state_dir=tmp_path, sixty60_latitude=-33.9249, sixty60_longitude=18.4241)
    )
    try:
        sixty60 = await application.services.search.search("milk", Retailer.SIXTY60, 1)
        woolworths = await application.services.search.search("milk", Retailer.WOOLWORTHS, 1)
        assert sixty60 and sixty60[Retailer.SIXTY60]
        assert woolworths and woolworths[Retailer.WOOLWORTHS]
    finally:
        await application.close()
