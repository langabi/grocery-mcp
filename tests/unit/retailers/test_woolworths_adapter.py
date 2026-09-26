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
                    "price": 39.99,
                }
            }
        ]
    return {
        "data": [
            {
                "id": "cart-1",
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
    def __init__(self, carts: list[dict[str, Any]] | None = None) -> None:
        self.auth = StubAuth()
        self._carts: Iterator[dict[str, Any]] = iter(carts or [])
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
        return {"productDisplayName": "Milk 2L", "price": 39.99, "isInStock": True}

    async def get_cart(self) -> dict[str, Any]:
        self.calls.append(("get_cart",))
        return next(self._carts)

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
async def test_writes_are_disabled_by_default_before_any_cart_read() -> None:
    client = StubClient()
    adapter = WoolworthsAdapter(client)  # type: ignore[arg-type]
    with pytest.raises(RetailerWriteDisabledError):
        await adapter.add_to_cart("600100", 1)
    assert client.calls == []


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
