from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse

from grocery_mcp.domain.change_sets import RequestedItem
from grocery_mcp.domain.models import Retailer, RetailerHealth
from grocery_mcp.mcp.schemas import (
    ComparisonResponse,
    ResolveHouseholdItemResponse,
    SearchResponse,
    ServiceHealth,
)
from grocery_mcp.retailers.base import (
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerUnavailableError,
    RetailerWriteDisabledError,
)
from grocery_mcp.services.cart_service import (
    CartService,
    ChangeSetNotFoundError,
    ChangeSetStateError,
)
from grocery_mcp.services.comparison import ComparisonService
from grocery_mcp.services.preferences import PreferenceService
from grocery_mcp.services.registry import AdapterRegistry
from grocery_mcp.services.search import SearchService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Services:
    adapters: AdapterRegistry
    search: SearchService
    comparison: ComparisonService
    preferences: PreferenceService
    carts: CartService


async def _safe[T](operation: Callable[[], Awaitable[T]]) -> T:
    try:
        return await operation()
    except Exception as exc:
        correlation_id = str(uuid4())
        code, message = _error_details(exc)
        logger.info("tool_error correlation_id=%s code=%s", correlation_id, code)
        raise ToolError(
            json.dumps(
                {"error": {"code": code, "message": message, "correlation_id": correlation_id}},
                separators=(",", ":"),
            )
        ) from None


def _error_details(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, ChangeSetNotFoundError):
        return "CHANGE_SET_NOT_FOUND", "The requested change set does not exist."
    if isinstance(exc, RetailerAuthenticationError):
        return "AUTH_REQUIRED", "Retailer authentication is required."
    if isinstance(exc, RetailerWriteDisabledError):
        return "WRITE_DISABLED", "Cart writes are disabled for this retailer."
    if isinstance(exc, RetailerUnavailableError):
        return "RETAILER_UNAVAILABLE", "The retailer is temporarily unavailable."
    if isinstance(exc, RetailerProtocolError):
        if "location" in str(exc).lower() or "delivery context" in str(exc).lower():
            return "LOCATION_REQUIRED", "A verified retailer delivery context is required."
        return "RETAILER_PROTOCOL_CHANGED", "The retailer response did not match its contract."
    if isinstance(exc, ChangeSetStateError):
        if "no longer available" in str(exc):
            return "PRODUCT_OUT_OF_STOCK", "A proposed product is no longer available."
        if "price is unavailable" in str(exc):
            return "PRICE_CHANGED", "A proposed product price can no longer be verified."
        return "CHANGE_SET_STATE", "The change set is not in an applicable state."
    if isinstance(exc, KeyError):
        return "PRODUCT_NOT_FOUND", "The requested household item or product was not found."
    if isinstance(exc, ValueError):
        if "no product found" in str(exc) or "price is unavailable" in str(exc):
            return "PRODUCT_NOT_FOUND", "No safely priced matching product was found."
        if "already matches" in str(exc):
            return "NO_CHANGE_REQUIRED", "The cart already holds the requested quantities."
        return "INVALID_REQUEST", "The request is invalid."
    return "INTERNAL_ERROR", "The operation failed safely."


def create_server(services: Services) -> MCPServer:
    server = MCPServer(
        "grocery-mcp",
        version="0.1.0",
        instructions=(
            "Search and prepare grocery carts. Cart writes require an immutable prepared "
            "change_set_id and a separate apply_cart_changes call after human approval. "
            "Never claim that this server can check out, place orders, or make payments."
        ),
    )

    @server.custom_route("/health", methods=["GET"])
    async def health_route(_request: Request) -> JSONResponse:
        """Unauthenticated liveness only; retailer readiness is an MCP tool."""
        return JSONResponse({"status": "ok"})

    @server.tool()
    async def search_products(
        query: str, retailer: Retailer | None = None, limit: int = 10
    ) -> dict[str, Any]:
        """Search retailer catalogues without changing a cart."""
        result = await _safe(lambda: services.search.search(query, retailer, limit))
        return SearchResponse(results=result).model_dump(mode="json")

    @server.tool()
    async def compare_products(
        query: str,
        retailers: list[Retailer] | None = None,
        limit_per_retailer: int = 5,
    ) -> dict[str, Any]:
        """Compare products and unit prices; warns when pack sizes are not comparable."""
        selected = retailers or [Retailer.SIXTY60, Retailer.WOOLWORTHS]
        result = await _safe(
            lambda: services.comparison.compare(query, selected, limit_per_retailer)
        )
        return ComparisonResponse(
            query=result.query, results=result.products, warnings=result.warnings
        ).model_dump(mode="json")

    @server.tool()
    async def compare_baskets(
        items: list[RequestedItem],
        retailers: list[Retailer] | None = None,
    ) -> dict[str, Any]:
        """Estimate a basket across retailers without changing any cart."""
        selected = retailers or [Retailer.SIXTY60, Retailer.WOOLWORTHS]
        estimates = await _safe(lambda: services.comparison.compare_baskets(items, selected))
        return {"estimates": [estimate.model_dump(mode="json") for estimate in estimates]}

    @server.tool()
    async def get_cart(retailer: Retailer) -> dict[str, Any]:
        """Read the current retailer cart. This tool never mutates it."""
        cart = await _safe(lambda: services.carts.get_cart(retailer))
        return cart.model_dump(mode="json")

    @server.tool()
    async def resolve_household_item(item: str, retailer: Retailer) -> dict[str, Any]:
        """Resolve an alias such as 'usual milk' to a preferred retailer product."""
        resolved = await _safe(lambda: services.preferences.resolve(item, retailer))
        return ResolveHouseholdItemResponse(
            item=resolved.item,
            retailer_mapping=resolved.mapping,
            warnings=resolved.warnings,
        ).model_dump(mode="json")

    @server.tool()
    async def prepare_cart_changes(
        retailer: Retailer, items: list[RequestedItem]
    ) -> dict[str, Any]:
        """Prepare and persist a cart proposal. This tool never mutates a real cart.

        Each item's mode decides the target quantity: "add" (the default) adds to what the
        cart already holds, while "set" makes quantity the exact cart quantity, so
        {"query": "milk", "quantity": 0, "mode": "set"} proposes removing the product.
        """
        change_set = await _safe(lambda: services.carts.prepare_changes(retailer, items))
        return change_set.model_dump(mode="json")

    @server.tool()
    async def apply_cart_changes(change_set_id: str) -> dict[str, Any]:
        """Apply exactly one prepared change set after explicit human approval."""
        result = await _safe(lambda: services.carts.apply_changes(change_set_id))
        return result.model_dump(mode="json")

    @server.tool()
    async def discard_cart_changes(change_set_id: str) -> dict[str, Any]:
        """Discard a prepared proposal without mutating a cart."""
        status = await _safe(lambda: services.carts.discard_changes(change_set_id))
        return {"change_set_id": change_set_id, "status": status.value}

    @server.tool()
    async def retailer_health() -> dict[str, Any]:
        """Report retailer capabilities separately for catalogue, auth, read, and write."""
        pairs = services.adapters.items()
        results = await asyncio.gather(
            *(adapter.health_check() for _, adapter in pairs), return_exceptions=True
        )
        health = [
            (
                RetailerHealth(
                    retailer=retailer,
                    catalogue=False,
                    authentication=False,
                    cart_read=False,
                    cart_write=False,
                    detail=f"health check failed ({type(result).__name__})",
                )
                if isinstance(result, BaseException)
                else result
            )
            for (retailer, _), result in zip(pairs, results, strict=True)
        ]
        response = ServiceHealth(
            status="ok" if health and all(result.catalogue for result in health) else "degraded",
            retailers=health,
        )
        return response.model_dump(mode="json")

    return server
