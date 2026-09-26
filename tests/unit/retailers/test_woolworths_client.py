from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from grocery_mcp.retailers.base import RetailerAuthenticationError, RetailerProtocolError
from grocery_mcp.retailers.woolworths.auth import (
    InMemoryWoolworthsSessionStore,
    WoolworthsAuth,
    WoolworthsSession,
)
from grocery_mcp.retailers.woolworths.client import WoolworthsClient, WoolworthsConfig


def valid_session(token: str = "token") -> WoolworthsSession:
    return WoolworthsSession(
        id_token=SecretStr(token),
        refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        dyn_user_id="customer-1",
    )


def make_client(http: httpx.AsyncClient, session: WoolworthsSession | None = None):
    config = WoolworthsConfig(
        cognito_url="https://cognito.test/",
        constructor_base="https://constructor.test",
        wfs_base="https://wfs.test/v4",
        sha1_password="apk-secret",
        place_id="place-1",
        store_id="store-1",
    )
    auth = WoolworthsAuth(
        http,
        InMemoryWoolworthsSessionStore(session),
        client_id="client-id",
        cognito_url=config.cognito_url,
    )
    return WoolworthsClient(http, auth, config=config)


@pytest.mark.asyncio
async def test_search_falls_back_to_autocomplete() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.startswith("/search/"):
            return httpx.Response(200, json={"response": {"results": []}})
        return httpx.Response(
            200,
            json={"sections": {"Products": [{"value": "Milk", "data": {"id": "1"}}]}},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await make_client(http).search("full cream", 3)
    assert len(result) == 1
    assert paths == ["/search/full cream", "/autocomplete/full cream"]


@pytest.mark.asyncio
async def test_wfs_retries_one_unauthorized_response_with_refresh() -> None:
    calls = 0

    class Auth:
        def __init__(self) -> None:
            self.refreshes = 0

        async def get_token(self) -> str:
            return "old-token"

        async def refresh(self) -> str:
            self.refreshes += 1
            return "new-token"

        def native_headers(self, token: str, *, sha1_password: SecretStr):
            return {"Sessiontoken": token, "Sha1password": sha1_password.get_secret_value()}

    auth = Auth()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            assert request.headers["Sessiontoken"] == "old-token"
            return httpx.Response(401, json={"secret": "must-not-leak"})
        assert request.headers["Sessiontoken"] == "new-token"
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WoolworthsClient(
            http,
            auth,  # type: ignore[arg-type]
            config=WoolworthsConfig(
                wfs_base="https://wfs.test/v4",
                sha1_password="secret",
                place_id="place-1",
                store_id="store-1",
            ),
        )
        assert await client.get_cart() == {"data": []}
    assert calls == 2
    assert auth.refreshes == 1


@pytest.mark.asyncio
async def test_second_unauthorized_response_is_redacted_auth_error() -> None:
    session = valid_session()

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"token": "do-not-leak"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = make_client(http, session)
        with pytest.raises(RetailerAuthenticationError) as caught:
            await client.get_cart()
    assert "do-not-leak" not in str(caught.value)


@pytest.mark.asyncio
async def test_success_with_non_json_body_is_protocol_error() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = make_client(http)
        with pytest.raises(RetailerProtocolError, match="invalid response"):
            await client.search("milk", 1)


@pytest.mark.asyncio
async def test_cart_mutation_request_shape_has_no_checkout_fields() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"ok": True})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await make_client(http, valid_session()).add_item("600100", 2)
    assert seen["path"] == "/v4/cart/OnDemand/itemV2"
    assert '"quantity":2' in seen["body"].replace(" ", "")
    assert "card" not in seen["body"].lower()
    assert "checkout" not in seen["body"].lower()


@pytest.mark.asyncio
async def test_wfs_sends_explicit_delivery_context() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["place"] = request.headers["Placeid"]
        seen["store"] = request.headers["Storeid"]
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await make_client(http, valid_session()).get_cart()

    assert seen == {"place": "place-1", "store": "store-1"}


@pytest.mark.asyncio
async def test_cart_read_fails_closed_without_delivery_context() -> None:
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)) as http:
        config = WoolworthsConfig(wfs_base="https://wfs.test/v4")
        auth = WoolworthsAuth(
            http,
            InMemoryWoolworthsSessionStore(valid_session()),
            client_id="client-id",
            cognito_url=config.cognito_url,
        )
        client = WoolworthsClient(http, auth, config=config)
        with pytest.raises(RetailerProtocolError, match="delivery context"):
            await client.get_cart()


@pytest.mark.asyncio
async def test_saved_locations_normalize_only_operator_selection_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v4/cart/checkout/savedAddresses"
        return httpx.Response(
            200,
            json={
                "savedAddresses": [
                    {
                        "nickname": "Home",
                        "placesId": "place-1",
                        "storeId": "store-1",
                        "fullAddress": "private address must not be returned",
                        "defaultAddress": True,
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        locations = await make_client(http, valid_session()).get_saved_locations()

    assert locations == [
        {
            "nickname": "Home",
            "place_id": "place-1",
            "store_id": "store-1",
            "is_default": True,
        }
    ]


@pytest.mark.asyncio
async def test_saved_locations_fall_back_to_legacy_endpoint() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cart/checkout/savedAddresses"):
            return httpx.Response(404, json={"error": "missing"})
        return httpx.Response(
            200,
            json={"addresses": [{"name": "Office", "placeId": "p2", "store_id": "s2"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        locations = await make_client(http, valid_session()).get_saved_locations()

    assert paths == ["/v4/cart/checkout/savedAddresses", "/v4/addresses"]
    assert locations == [
        {"nickname": "Office", "place_id": "p2", "store_id": "s2", "is_default": False}
    ]


@pytest.mark.asyncio
async def test_saved_locations_resolve_missing_store_from_confirm_location() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cart/checkout/savedAddresses"):
            return httpx.Response(
                200,
                json={
                    "addresses": [
                        {"nickname": "Home", "placesId": "place-1", "verified": True}
                    ],
                    # The live API does not always match this value to the sole address nickname.
                    "defaultAddressNickname": "Primary address",
                },
            )
        assert request.method == "POST"
        assert json.loads(request.content) == {
            "address": {"nickname": "Home", "placeId": "place-1"},
            "deliveryType": "OnDemand",
            "page": "checkout",
            "storeId": "",
        }
        return httpx.Response(200, json={"deliveryContext": {"storeId": "3162"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        locations = await make_client(http, valid_session()).get_saved_locations()

    assert paths == [
        "/v4/cart/checkout/savedAddresses",
        "/v4/cartV2/confirmLocation",
    ]
    assert locations == [
        {
            "nickname": "Home",
            "place_id": "place-1",
            "store_id": "3162",
            "is_default": True,
        }
    ]
