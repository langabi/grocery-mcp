from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import SecretStr

from grocery_mcp.retailers.base import RetailerAuthenticationError, RetailerProtocolError
from grocery_mcp.retailers.woolworths.auth import (
    InMemoryWoolworthsSessionStore,
    PersistentWoolworthsSessionStore,
    WoolworthsAuth,
    WoolworthsCredentials,
    WoolworthsSession,
)


def token(*, expires_at: datetime, user_id: str = "customer-1") -> str:
    payload = json.dumps({"exp": int(expires_at.timestamp()), "custom:AtgId": user_id}).encode()
    encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
    return f"header.{encoded}.signature"


@pytest.mark.asyncio
async def test_login_persists_tokens_without_exposing_secret_repr() -> None:
    expires = datetime.now(UTC) + timedelta(hours=1)
    id_token = token(expires_at=expires)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "AuthenticationResult": {
                    "IdToken": id_token,
                    "RefreshToken": "refresh-secret",
                }
            },
        )

    store = InMemoryWoolworthsSessionStore()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = WoolworthsAuth(
            http,
            store,
            client_id="client-id",
            cognito_url="https://cognito.test/",
            credentials=WoolworthsCredentials(email="person@example.test", password="password"),
        )
        assert await auth.get_token() == id_token

    assert seen["AuthFlow"] == "USER_PASSWORD_AUTH"
    assert store.session is not None
    assert store.session.dyn_user_id == "customer-1"
    assert "refresh-secret" not in repr(store.session)
    assert "password='password'" not in repr(auth._credentials)  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_expired_session_refreshes_and_keeps_refresh_token() -> None:
    old = WoolworthsSession(
        id_token=SecretStr(token(expires_at=datetime.now(UTC) - timedelta(minutes=1))),
        refresh_token=SecretStr("refresh-secret"),
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    new_token = token(expires_at=datetime.now(UTC) + timedelta(hours=1))

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["AuthFlow"] == "REFRESH_TOKEN_AUTH"
        assert body["AuthParameters"] == {"REFRESH_TOKEN": "refresh-secret"}
        return httpx.Response(200, json={"AuthenticationResult": {"IdToken": new_token}})

    store = InMemoryWoolworthsSessionStore(old)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = WoolworthsAuth(
            http, store, client_id="client-id", cognito_url="https://cognito.test/"
        )
        assert await auth.get_token() == new_token
    assert store.session is not None
    assert store.session.refresh_token is not None
    assert store.session.refresh_token.get_secret_value() == "refresh-secret"


@pytest.mark.asyncio
async def test_missing_credentials_is_typed_and_redacted() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(500))
    async with httpx.AsyncClient(transport=transport) as http:
        auth = WoolworthsAuth(
            http,
            InMemoryWoolworthsSessionStore(),
            client_id="client-id",
            cognito_url="https://cognito.test/",
        )
        with pytest.raises(RetailerAuthenticationError, match="authentication is required"):
            await auth.get_token()


@pytest.mark.asyncio
async def test_invalid_token_response_is_protocol_error_without_body() -> None:
    secret = "do-not-leak"

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"AuthenticationResult": {"IdToken": secret}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        auth = WoolworthsAuth(
            http,
            InMemoryWoolworthsSessionStore(),
            client_id="client-id",
            cognito_url="https://cognito.test/",
            credentials=WoolworthsCredentials(email="person@example.test", password="password"),
        )
        with pytest.raises(RetailerProtocolError) as caught:
            await auth.login()
    assert secret not in str(caught.value)


@pytest.mark.asyncio
async def test_persistent_store_bridge_round_trips_actual_secret_values() -> None:
    class Backend:
        payload: dict[str, object] | None = None

        async def load_session(self, retailer: object) -> dict[str, object] | None:
            assert str(retailer) == "woolworths"
            return self.payload

        async def save_session(self, retailer: object, payload: dict[str, object]) -> None:
            assert str(retailer) == "woolworths"
            self.payload = payload

    backend = Backend()
    store = PersistentWoolworthsSessionStore(backend)  # type: ignore[arg-type]
    session = WoolworthsSession(
        id_token=SecretStr("id-secret"),
        refresh_token=SecretStr("refresh-secret"),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        dyn_user_id="customer-1",
    )
    await store.save(session)
    assert backend.payload is not None
    assert backend.payload["id_token"] == "id-secret"
    loaded = await store.load()
    assert loaded is not None
    assert loaded.id_token.get_secret_value() == "id-secret"


@pytest.mark.asyncio
async def test_invalid_persisted_session_error_redacts_payload() -> None:
    secret = "stored-token-must-not-leak"

    class Backend:
        async def load_session(self, retailer: object) -> dict[str, object]:
            return {"id_token": secret}

        async def save_session(self, retailer: object, payload: dict[str, object]) -> None:
            raise AssertionError("not called")

    store = PersistentWoolworthsSessionStore(Backend())  # type: ignore[arg-type]
    with pytest.raises(RetailerProtocolError) as caught:
        await store.load()
    assert secret not in str(caught.value)
