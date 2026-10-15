from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from grocery_mcp.domain.models import Retailer
from grocery_mcp.retailers.base import RetailerProtocolError, RetailerWriteDisabledError
from grocery_mcp.retailers.woolworths.adapter import WoolworthsAdapter


def cart_payload(quantity: int | None = None) -> dict[str, Any]:
    lines = []
    if quantity is not None:
        lines = [
            {
                "commerceItemInfo": {
                    "id": "ci-1",
                    "catalogRefId": "600100",
                    "productDisplayName": "Ayrshire Full Cream Milk 2L",
                    "quantity": quantity,
                },
                "priceInfo": {
                    "amount": quantity * 39.99,
                    "listPrice": 39.99,
                    "salePrice": 0.0,
                },
            }
        ]
    return {
        "data": [
            {
                "basketId": "cart-1",
                "items": {"foodCommerceItem": lines},
                "orderSummary": {"total": (quantity or 0) * 39.99},
            }
        ]
    }


class StubAuth:
    can_authenticate = True

    async def has_persisted_session(self) -> bool:
        return True


class StubClient:
    def __init__(
        self,
        carts: list[dict[str, Any]] | None = None,
        orders: dict[str, Any] | None = None,
        order_details: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.auth = StubAuth()
        self._carts: Iterator[dict[str, Any]] = iter(carts or [])
        self._orders = orders or {"upcomingOrders": [], "pastOrders": []}
        self._order_details = order_details or {}
        self.calls: list[tuple] = []

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append(("search", query, limit))
        return [
            {
                "value": "Ayrshire Full Cream Milk 2L",
                "data": {
                    "id": "600100",
                    "price": "39.99",
                    "brand": "Woolworths",
                    "image_url": "https://images.test/milk.jpg",
                },
            }
        ]

    async def product_detail(self, sku: str) -> dict[str, Any]:
        return {
            "productId": sku,
            "sku": sku,
            "productName": "Ayrshire Full Cream Milk 2 L",
            "brandText": "",
            "price": "39.99",
            "isnAvailable": "true",
            "externalImageRefV2": "https://images.test/milk.jpg",
        }

    async def get_cart(self) -> dict[str, Any]:
        self.calls.append(("get_cart",))
        return next(self._carts)

    async def get_orders(self) -> dict[str, Any]:
        self.calls.append(("get_orders",))
        return self._orders

    async def get_order_detail(self, order_id: str) -> dict[str, Any]:
        self.calls.append(("get_order_detail", order_id))
        return self._order_details[order_id]

    async def add_item(self, sku: str, quantity: int) -> None:
        self.calls.append(("add", sku, quantity))

    async def set_quantity(self, commerce_id: str, quantity: int) -> None:
        self.calls.append(("set", commerce_id, quantity))

    async def remove_item(self, commerce_id: str) -> None:
        self.calls.append(("remove", commerce_id))


@pytest.mark.asyncio
async def test_maps_constructor_search_to_canonical_product() -> None:
    client = StubClient()
    products = await WoolworthsAdapter(client).search_products(" milk ", 5)  # type: ignore[arg-type]
    assert len(products) == 1
    product = products[0]
    assert product.retailer is Retailer.WOOLWORTHS
    assert product.retailer_product_id == "600100"
    assert str(product.price) == "39.99"
    assert product.size is not None
    assert str(product.size.value) == "2"
    assert product.size.unit == "L"
    assert client.calls == [("search", "milk", 5)]


@pytest.mark.asyncio
async def test_maps_product_detail_without_inventing_stock() -> None:
    product = await WoolworthsAdapter(StubClient()).get_product(" 600100 ")  # type: ignore[arg-type]

    assert product.retailer_product_id == "600100"
    assert product.name == "Ayrshire Full Cream Milk 2 L"
    assert str(product.price) == "39.99"
    assert product.brand is None
    assert product.in_stock is None
    assert product.image_url == "https://images.test/milk.jpg"


@pytest.mark.asyncio
async def test_writes_are_disabled_by_default_before_any_cart_read() -> None:
    client = StubClient()
    adapter = WoolworthsAdapter(client)  # type: ignore[arg-type]
    with pytest.raises(RetailerWriteDisabledError):
        await adapter.add_to_cart("600100", 1)
    assert client.calls == []


def order_summary(order_id: str, submitted: str, **overrides: Any) -> dict[str, Any]:
    return {
        "orderId": order_id,
        "state": "DELIVERED",
        "submittedDate": submitted,
        "completedDate": submitted,
        "total": 129.99,
        "totalItemsCount": 3,
        "deliveryType": "OnDemand",
        "deliveryAddress": {"address1": "must not be returned"},
        **overrides,
    }


def order_line(
    product_id: str, name: str, quantity: int, list_price: float, amount: float
) -> dict[str, Any]:
    return {
        "commerceItemInfo": {
            "id": f"ci-{product_id}",
            "catalogRefId": product_id,
            "productDisplayName": name,
            "quantity": quantity,
        },
        "priceInfo": {"amount": amount, "listPrice": list_price, "salePrice": 0.0},
    }


@pytest.mark.asyncio
async def test_order_history_is_normalized_and_limited() -> None:
    client = StubClient(
        orders={
            "upcomingOrders": [order_summary("o-3", "14/10/2026 12:00:00", state="PLACED")],
            "pastOrders": [
                order_summary("o-2", "11/10/2026 12:00:00"),
                order_summary("o-1", "02/08/2026 12:00:00"),
            ],
            "totalOrderCount": 3,
        }
    )

    orders = await WoolworthsAdapter(client).list_orders(limit=2)  # type: ignore[arg-type]

    assert [order.order_id for order in orders] == ["o-3", "o-2"]
    assert orders[0].status == "PLACED"
    assert str(orders[1].total) == "129.99"
    assert orders[1].item_count == 3
    assert orders[1].created_at is not None
    assert orders[1].created_at.isoformat() == "2026-10-11T12:00:00+02:00"
    assert orders[1].items == []
    assert "address" not in orders[1].model_dump_json()
    assert client.calls == [("get_orders",)]


@pytest.mark.asyncio
async def test_order_history_rejects_an_unrecognised_date() -> None:
    client = StubClient(orders={"pastOrders": [order_summary("o-1", "2026-10-11T12:00:00Z")]})

    with pytest.raises(RetailerProtocolError):
        await WoolworthsAdapter(client).list_orders()  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_get_order_takes_line_items_from_the_order_detail() -> None:
    client = StubClient(
        orders={"pastOrders": [order_summary("o-1", "11/10/2026 12:00:00")]},
        order_details={
            "o-1": {
                "orderSummary": {"orderId": "o-1", "total": 999.99},
                "items": {
                    "food": [
                        order_line("6001", "White Hamburger Rolls 6 pk", 2, 28.99, 57.98),
                        order_line("6002", "Baby Spinach 100 g", 2, 19.99, 30.0),
                        order_line("6003", "Substituted Chicken", 0, 167.9895, 0.0),
                    ]
                },
            }
        },
    )

    order = await WoolworthsAdapter(client).get_order(" o-1 ")  # type: ignore[arg-type]

    assert str(order.total) == "129.99"
    rolls, spinach, substituted = order.items
    assert (rolls.retailer_product_id, rolls.quantity) == ("6001", 2)
    assert (str(rolls.unit_price), str(rolls.line_total)) == ("28.99", "57.98")
    assert (str(spinach.unit_price), str(spinach.line_total)) == ("19.99", "30.0")
    assert substituted.quantity == 0
    assert client.calls == [("get_orders",), ("get_order_detail", "o-1")]


@pytest.mark.asyncio
async def test_get_unknown_order_raises_key_error_without_detail_call() -> None:
    client = StubClient(orders={"pastOrders": [order_summary("o-1", "11/10/2026 12:00:00")]})

    with pytest.raises(KeyError):
        await WoolworthsAdapter(client).get_order("o-404")  # type: ignore[arg-type]
    assert client.calls == [("get_orders",)]


@pytest.mark.asyncio
async def test_cart_unit_price_is_the_list_price_not_the_line_amount() -> None:
    cart = await WoolworthsAdapter(StubClient([cart_payload(2)])).get_cart()  # type: ignore[arg-type]

    assert cart.cart_id == "cart-1"
    assert str(cart.items[0].unit_price) == "39.99"


@pytest.mark.asyncio
async def test_add_new_item_is_additive_once_then_verified() -> None:
    client = StubClient([cart_payload(), cart_payload(2)])
    cart = await WoolworthsAdapter(client, write_enabled=True).add_to_cart(  # type: ignore[arg-type]
        "600100", 2
    )
    assert cart.items[0].quantity == 2
    assert client.calls == [("get_cart",), ("add", "600100", 2), ("get_cart",)]


@pytest.mark.asyncio
async def test_add_existing_item_uses_absolute_quantity() -> None:
    client = StubClient([cart_payload(2), cart_payload(3)])
    await WoolworthsAdapter(client, write_enabled=True).add_to_cart("600100", 1)  # type: ignore[arg-type]
    assert ("set", "ci-1", 3) in client.calls
    assert not any(call[0] == "add" for call in client.calls)


@pytest.mark.asyncio
async def test_remove_uses_commerce_id_and_verifies_absence() -> None:
    client = StubClient([cart_payload(1), cart_payload()])
    cart = await WoolworthsAdapter(client, write_enabled=True).remove_from_cart(  # type: ignore[arg-type]
        "600100"
    )
    assert cart.items == []
    assert ("remove", "ci-1") in client.calls


@pytest.mark.asyncio
async def test_failed_read_after_write_verification_is_typed() -> None:
    client = StubClient([cart_payload(), cart_payload(1)])
    adapter = WoolworthsAdapter(client, write_enabled=True)  # type: ignore[arg-type]
    with pytest.raises(RetailerProtocolError, match="could not be verified"):
        await adapter.add_to_cart("600100", 2)


@pytest.mark.asyncio
async def test_health_does_not_advertise_write_when_disabled() -> None:
    client = StubClient([cart_payload()])
    health = await WoolworthsAdapter(client).health_check()  # type: ignore[arg-type]
    assert health.catalogue is True
    assert health.authentication is True
    assert health.cart_read is True
    assert health.cart_write is False
    assert health.order_history is True


@pytest.mark.parametrize("quantity", [0, -1, True, 1.5])
@pytest.mark.asyncio
async def test_add_rejects_invalid_quantity_without_network(quantity: Any) -> None:
    client = StubClient()
    adapter = WoolworthsAdapter(client, write_enabled=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await adapter.add_to_cart("600100", quantity)
    assert client.calls == []


async def test_two_untyped_carts_are_refused_rather_than_guessed() -> None:
    payload = cart_payload(1)
    payload["data"].append({"id": "cart-2", "items": {}, "orderSummary": {"total": 0}})
    adapter = WoolworthsAdapter(StubClient([payload]))  # type: ignore[arg-type]

    with pytest.raises(RetailerProtocolError, match="more than one cart"):
        await adapter.get_cart()


async def test_the_on_demand_cart_is_selected_when_delivery_types_differ() -> None:
    payload = cart_payload(1)
    payload["data"][0]["deliveryType"] = "OnDemand"
    payload["data"].append(
        {
            "id": "cart-2",
            "deliveryType": "Standard",
            "items": {},
            "orderSummary": {"total": 0},
        }
    )
    adapter = WoolworthsAdapter(StubClient([payload]))  # type: ignore[arg-type]

    cart = await adapter.get_cart()

    assert cart.cart_id == "cart-1"
