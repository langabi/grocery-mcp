from __future__ import annotations

from pathlib import Path

from mcp import Client

from grocery_mcp.app import build_application
from grocery_mcp.config import Settings


async def test_mcp_server_negotiates_and_exposes_only_bounded_tools(tmp_path: Path) -> None:
    application = await build_application(Settings(state_dir=tmp_path))
    try:
        async with Client(application.server) as client:
            result = await client.list_tools()
            names = {tool.name for tool in result.tools}

            assert client.protocol_version == "2026-07-28"
            assert names == {
                "search_products",
                "compare_products",
                "compare_baskets",
                "get_cart",
                "resolve_household_item",
                "prepare_cart_changes",
                "apply_cart_changes",
                "discard_cart_changes",
                "retailer_health",
            }
            assert not names.intersection(
                {"checkout", "place_order", "pay", "add_to_cart", "remove_from_cart"}
            )

            failure = await client.call_tool(
                "resolve_household_item", {"item": "missing", "retailer": "sixty60"}
            )
            assert failure.is_error is True
            message = failure.content[0].text
            assert '"code":"PRODUCT_NOT_FOUND"' in message
            assert '"correlation_id":' in message
    finally:
        await application.close()
