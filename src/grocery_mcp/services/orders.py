from __future__ import annotations

from grocery_mcp.domain.models import Retailer, RetailerOrder
from grocery_mcp.services.registry import AdapterRegistry


class OrderNotFoundError(LookupError):
    pass


class OrderService:
    def __init__(self, adapters: AdapterRegistry) -> None:
        self.adapters = adapters

    async def list_orders(self, retailer: Retailer, limit: int = 20) -> list[RetailerOrder]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return await self.adapters.get(retailer).list_orders(limit)

    async def get_order(self, retailer: Retailer, order_id: str) -> RetailerOrder:
        order_id = order_id.strip()
        if not order_id:
            raise ValueError("order_id must not be empty")
        try:
            return await self.adapters.get(retailer).get_order(order_id)
        except KeyError:
            raise OrderNotFoundError(order_id) from None
