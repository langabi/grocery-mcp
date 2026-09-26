from __future__ import annotations

import json

import httpx
import pytest

from grocery_mcp.retailers.base import (
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerWriteDisabledError,
)
from grocery_mcp.retailers.sixty60 import (
    MemorySessionStore,
    Sixty60Adapter,
    Sixty60Client,
    Sixty60Config,
    Sixty60Session,
)

BASES = {
    "bff_base_url": "https://bff.test",
    "dsl_base_url": "https://dsl.test",
    "auth_base_url": "https://auth.test",
    "catalog_base_url": "https://catalog.test",
    "orders_base_url": "https://orders.test",
}


def config(**overrides: object) -> Sixty60Config:
    return Sixty60Config(**BASES, **overrides)  # type: ignore[arg-type]


def session(*, cart_id: str | None = "cart-1") -> Sixty60Session:
    return Sixty60Session(
        phone_e164="+27821234567",
        customer_id="customer-1",
        user_id="user-1",
        email="person@example.test",
        access_token="secret-access-token",
        refresh_token="secret-refresh-token",
        store_ids=["store-1"],
        cart_id=cart_id,
    )


def adapter_with_handler(
    handler: httpx.MockTransport,
    *,
    stored_session: Sixty60Session | None = None,
    write_enabled: bool = False,
    **config_values: object,
) -> Sixty60Adapter:
    http = httpx.AsyncClient(transport=handler)
    client = Sixty60Client(config(**config_values), http)
    return Sixty60Adapter(client, MemorySessionStore(stored_session), write_enabled=write_enabled)


@pytest.mark.asyncio
async def test_anonymous_location_aware_search_maps_products() -> None:
    requests: list[httpx.Request] = []

    def route(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/token/dsl":
            return httpx.Response(200, json={"access_token": "anonymous-token"})
        if request.url.path == "/api/v3/store-contexts":
            assert json.loads(request.content) == {"latitude": -33.9, "longitude": 18.4}
            return httpx.Response(200, json={"items": [{"storeId": "store-1"}]})
        if request.url.path == "/api/v3/products/product-list-page":
            body = json.loads(request.content)
            assert body["filter"]["productListSource"] == {"search": "milk"}
            assert body["userContext"]["storeContexts"][0]["storeId"] == "store-1"
            return httpx.Response(
                200,
                json={
                    "products": [
                        {
                            "id": "prod-1",
                            "sku": "sku-1",
                            "name": "Fresh Milk 2L",
                            "priceWithoutDecimal": 3999,
                            "priceFactor": 100,
                            "isStockAvailable": True,
                            "storeId": "store-1",
                        }
                    ]
                },
            )
        raise AssertionError(f"Unexpected request: {request.url}")

    adapter = adapter_with_handler(httpx.MockTransport(route), latitude=-33.9, longitude=18.4)
    products = await adapter.search_products(" milk ", limit=5)

    assert len(products) == 1
    assert products[0].retailer_product_id == "prod-1"
    assert str(products[0].price) == "39.99"
    assert products[0].store_id == "store-1"
    assert "mobileNumber" not in requests[-1].headers


@pytest.mark.asyncio
async def test_login_flow_persists_session_without_exposing_tokens_in_repr() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/v1/token/dsl":
            return httpx.Response(200, json={"access_token": "bootstrap-secret"})
        if path == "/users/verify":
            return httpx.Response(200, json={"response": {"uid": "customer-1"}})
        if path == "/users/loginbymobile":
            return httpx.Response(200, json={"response": {"reference": "otp-ref"}})
        if path == "/otp/loginbymobile/verify":
            return httpx.Response(
                200,
                json={
                    "response": {
                        "accessToken": "access-secret",
                        "refreshToken": "refresh-secret",
                    }
                },
            )
        if path.startswith("/customers/"):
            return httpx.Response(
                200,
                json={"userProfile": {"id": "user-1", "email": "person@example.test"}},
            )
        if path == "/api/v3/store-contexts":
            return httpx.Response(200, json={"items": [{"storeId": "store-1"}]})
        raise AssertionError(path)

    store = MemorySessionStore()
    client = Sixty60Client(
        config(dsl_api_key="dsl-key", auth_api_key="auth-key", profile_api_token="profile-key"),
        httpx.AsyncClient(transport=httpx.MockTransport(route)),
    )
    adapter = Sixty60Adapter(client, store)

    challenge = await adapter.start_login("082 123 4567")
    logged_in = await adapter.complete_login(challenge, "1234")

    assert challenge.phone_e164 == "+27821234567"
    assert (await store.load()) == logged_in
    assert logged_in.store_ids == ["store-1"]
    assert "access-secret" not in repr(logged_in)
    assert "refresh-secret" not in repr(logged_in)


@pytest.mark.asyncio
async def test_http_failures_are_typed_and_redact_body_and_token() -> None:
    secret = "very-secret-token"

    def route(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"upstream leaked {secret}")

    adapter = adapter_with_handler(httpx.MockTransport(route), stored_session=session())
    with pytest.raises(RetailerAuthenticationError) as raised:
        await adapter.get_cart()
    assert secret not in str(raised.value)
    assert "upstream leaked" not in str(raised.value)


@pytest.mark.asyncio
async def test_write_is_disabled_by_default_before_any_network_call() -> None:
    called = False

    def route(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    adapter = adapter_with_handler(httpx.MockTransport(route), stored_session=session())
    with pytest.raises(RetailerWriteDisabledError):
        await adapter.add_to_cart("prod-1", 1)
    assert called is False


@pytest.mark.asyncio
async def test_write_requires_an_explicitly_pinned_cart() -> None:
    adapter = adapter_with_handler(
        httpx.MockTransport(lambda _: httpx.Response(500)),
        stored_session=session(cart_id=None),
        write_enabled=True,
    )
    with pytest.raises(RetailerWriteDisabledError, match="explicitly pinned"):
        await adapter.set_quantity("prod-1", 2)


@pytest.mark.asyncio
async def test_cart_read_refuses_ambiguous_unpinned_carts() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "catalog.test":
            return httpx.Response(200, json={"items": [{"storeId": "store-1"}]})
        return httpx.Response(
            200,
            json={
                "carts": [
                    {"item": {"id": "a", "serviceOptionId": "sixty-min-delivery"}},
                    {"item": {"id": "b", "serviceOptionId": "sixty-min-delivery"}},
                ]
            },
        )

    adapter = adapter_with_handler(httpx.MockTransport(route), stored_session=session(cart_id=None))
    with pytest.raises(RetailerProtocolError, match="ambiguous"):
        await adapter.get_cart()


@pytest.mark.asyncio
async def test_set_quantity_rewrites_exact_cart_and_verifies_readback() -> None:
    cart_reads = 0
    update_body: dict[str, object] = {}

    def route(request: httpx.Request) -> httpx.Response:
        nonlocal cart_reads, update_body
        if request.url.host == "catalog.test":
            return httpx.Response(200, json={"items": [{"storeId": "store-1"}]})
        if request.url.path == "/api/v2/carts/user":
            cart_reads += 1
            quantity = 1 if cart_reads == 1 else 3
            return httpx.Response(
                200,
                json={
                    "carts": [
                        {
                            "item": {
                                "id": "cart-1",
                                "serviceOptionId": "sixty-min-delivery",
                                "deliveryAddress": {"identifier": "address-1"},
                                "lineItems": [
                                    {
                                        "id": "line-1",
                                        "productId": "prod-1",
                                        "name": "Milk",
                                        "quantity": quantity,
                                        "price": 3000,
                                        "priceFactor": 100,
                                        "storeId": "store-1",
                                    }
                                ],
                            }
                        },
                        {
                            "item": {
                                "id": "cart-other",
                                "serviceOptionId": "collection",
                                "lineItems": [],
                            }
                        },
                    ]
                },
            )
        if request.url.path == "/api/v3/carts/update":
            update_body = json.loads(request.content)
            return httpx.Response(200, json={"ok": True})
        if "update-promotions" in request.url.path:
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(request.url)

    adapter = adapter_with_handler(
        httpx.MockTransport(route), stored_session=session(), write_enabled=True
    )
    cart = await adapter.set_quantity("prod-1", 3)

    assert cart_reads == 2
    assert cart.items[0].quantity == 3
    assert update_body["deliveryAddressId"] == "address-1"
    submitted = update_body["carts"]
    assert isinstance(submitted, list)
    target = next(item for item in submitted if item["id"] == "cart-1")
    assert target["lineItems"][0]["quantity"] == 3
    other = next(item for item in submitted if item["id"] == "cart-other")
    assert other["lineItems"] == []


@pytest.mark.asyncio
async def test_failed_write_verification_raises_protocol_error() -> None:
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.host == "catalog.test":
            return httpx.Response(200, json={"items": [{"storeId": "store-1"}]})
        if request.url.path == "/api/v2/carts/user":
            return httpx.Response(
                200,
                json={
                    "carts": [
                        {
                            "item": {
                                "id": "cart-1",
                                "serviceOptionId": "sixty-min-delivery",
                                "deliveryAddress": {"identifier": "address-1"},
                                "lineItems": [
                                    {
                                        "productId": "prod-1",
                                        "name": "Milk",
                                        "quantity": 1,
                                        "price": 3000,
                                    }
                                ],
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"ok": True})

    adapter = adapter_with_handler(
        httpx.MockTransport(route), stored_session=session(), write_enabled=True
    )
    with pytest.raises(RetailerProtocolError, match="could not be verified"):
        await adapter.set_quantity("prod-1", 3)
