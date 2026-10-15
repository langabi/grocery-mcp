from __future__ import annotations

import pytest

from grocery_mcp.domain.models import Retailer, RetailerOrder
from grocery_mcp.services.orders import OrderNotFoundError, OrderService
from grocery_mcp.services.registry import AdapterRegistry


class StubOrderAdapter:
    async def list_orders(self, limit: int = 20) -> list[RetailerOrder]:
        return [
            RetailerOrder(
                retailer=Retailer.SIXTY60,
                order_id="order-1",
                status="delivered",
            )
        ][:limit]

    async def get_order(self, order_id: str) -> RetailerOrder:
        if order_id != "order-1":
            raise KeyError(order_id)
        return (await self.list_orders())[0]


def service() -> OrderService:
    registry = AdapterRegistry(  # type: ignore[arg-type]
        {Retailer.SIXTY60: StubOrderAdapter()}
    )
    return OrderService(registry)


async def test_order_service_lists_and_gets_orders() -> None:
    orders = await service().list_orders(Retailer.SIXTY60, 1)
    order = await service().get_order(Retailer.SIXTY60, " order-1 ")

    assert [item.order_id for item in orders] == ["order-1"]
    assert order.order_id == "order-1"


async def test_order_service_returns_specific_not_found_error() -> None:
    with pytest.raises(OrderNotFoundError):
        await service().get_order(Retailer.SIXTY60, "missing")


@pytest.mark.parametrize("limit", [0, 101, True])
async def test_order_service_rejects_invalid_limit(limit: int) -> None:
    with pytest.raises(ValueError):
        await service().list_orders(Retailer.SIXTY60, limit)
