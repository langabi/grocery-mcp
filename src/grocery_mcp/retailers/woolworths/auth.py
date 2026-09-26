from __future__ import annotations

import base64
import binascii
import json
from datetime import UTC, datetime, timedelta
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError, field_validator

from grocery_mcp.domain.models import Retailer
from grocery_mcp.retailers.base import (
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerUnavailableError,
)


class WoolworthsSession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id_token: SecretStr
    refresh_token: SecretStr | None = None
    expires_at: datetime
    dyn_user_id: str = ""

    @field_validator("expires_at")
    @classmethod
    def timezone_aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("expires_at must be timezone-aware")
        return value

    def is_valid(self, now: datetime, *, leeway: timedelta = timedelta(seconds=60)) -> bool:
        return now < self.expires_at - leeway


class WoolworthsCredentials(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str
    password: SecretStr


class WoolworthsSessionStore(Protocol):
    async def load(self) -> WoolworthsSession | None: ...

    async def save(self, session: WoolworthsSession) -> None: ...


class RetailerStateBackend(Protocol):
    async def load_session(self, retailer: Retailer | str) -> dict[str, object] | None: ...

    async def save_session(self, retailer: Retailer | str, payload: dict[str, object]) -> None: ...


class PersistentWoolworthsSessionStore:
    """Typed bridge to the application's encrypted retailer-state backend."""

    def __init__(self, backend: RetailerStateBackend) -> None:
        self._backend = backend

    async def load(self) -> WoolworthsSession | None:
        payload = await self._backend.load_session(Retailer.WOOLWORTHS)
        if payload is None:
            return None
        try:
            return WoolworthsSession.model_validate(payload)
        except ValidationError as exc:
            raise RetailerProtocolError("Stored Woolworths session was invalid") from exc

    async def save(self, session: WoolworthsSession) -> None:
        await self._backend.save_session(
            Retailer.WOOLWORTHS,
            {
                "id_token": session.id_token.get_secret_value(),
                "refresh_token": (
                    session.refresh_token.get_secret_value() if session.refresh_token else None
                ),
                "expires_at": session.expires_at.isoformat(),
                "dyn_user_id": session.dyn_user_id,
            },
        )


class InMemoryWoolworthsSessionStore:
    def __init__(self, session: WoolworthsSession | None = None) -> None:
        self.session = session

    async def load(self) -> WoolworthsSession | None:
        return self.session

    async def save(self, session: WoolworthsSession) -> None:
        self.session = session


class _AuthenticationResult(BaseModel):
    model_config = ConfigDict(extra="ignore")

    IdToken: SecretStr
    RefreshToken: SecretStr | None = None


class _CognitoResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    AuthenticationResult: _AuthenticationResult


class WoolworthsAuth:
    """Cognito token manager; secrets are only returned to the WFS client."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        store: WoolworthsSessionStore,
        *,
        client_id: str,
        cognito_url: str,
        credentials: WoolworthsCredentials | None = None,
        timeout: httpx.Timeout | float = 15.0,
    ) -> None:
        self._http = http
        self._store = store
        self._client_id = client_id
        self._cognito_url = cognito_url
        self._credentials = credentials
        self._timeout = timeout
        self._session: WoolworthsSession | None = None
        self._loaded = False

    @property
    def can_authenticate(self) -> bool:
        return self._session is not None or self._credentials is not None

    async def has_persisted_session(self) -> bool:
        await self._load_once()
        return self._session is not None

    async def get_token(self) -> str:
        await self._load_once()
        now = datetime.now(UTC)
        if self._session is not None and self._session.is_valid(now):
            return self._session.id_token.get_secret_value()
        return await self.refresh()

    async def login(self) -> str:
        if self._credentials is None:
            raise RetailerAuthenticationError("Woolworths authentication is required")
        response = await self._cognito(
            "USER_PASSWORD_AUTH",
            {
                "USERNAME": self._credentials.email,
                "PASSWORD": self._credentials.password.get_secret_value(),
            },
        )
        return await self._apply(response, keep_refresh=False)

    async def refresh(self) -> str:
        await self._load_once()
        if self._session is not None and self._session.refresh_token is not None:
            try:
                response = await self._cognito(
                    "REFRESH_TOKEN_AUTH",
                    {"REFRESH_TOKEN": self._session.refresh_token.get_secret_value()},
                )
                return await self._apply(response, keep_refresh=True)
            except RetailerAuthenticationError:
                if self._credentials is None:
                    raise
        return await self.login()

    async def _load_once(self) -> None:
        if not self._loaded:
            self._session = await self._store.load()
            self._loaded = True

    async def _cognito(self, flow: str, parameters: dict[str, str]) -> _CognitoResponse:
        try:
            response = await self._http.post(
                self._cognito_url,
                headers={
                    "Content-Type": "application/x-amz-json-1.1",
                    "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
                },
                json={
                    "AuthFlow": flow,
                    "ClientId": self._client_id,
                    "AuthParameters": parameters,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise RetailerUnavailableError("Woolworths authentication timed out") from exc
        except httpx.HTTPError as exc:
            raise RetailerUnavailableError("Woolworths authentication is unavailable") from exc
        if response.status_code in {400, 401, 403}:
            raise RetailerAuthenticationError("Woolworths credentials or session were rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetailerUnavailableError("Woolworths authentication is unavailable")
        if not 200 <= response.status_code < 300:
            raise RetailerProtocolError("Woolworths authentication returned an unexpected status")
        try:
            return _CognitoResponse.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise RetailerProtocolError("Woolworths authentication response was invalid") from exc

    async def _apply(self, response: _CognitoResponse, *, keep_refresh: bool) -> str:
        auth = response.AuthenticationResult
        token = auth.IdToken.get_secret_value()
        expiry, dyn_user_id = _claims(token)
        refresh_token = auth.RefreshToken
        if keep_refresh and refresh_token is None and self._session is not None:
            refresh_token = self._session.refresh_token
        session = WoolworthsSession(
            id_token=auth.IdToken,
            refresh_token=refresh_token,
            expires_at=expiry,
            dyn_user_id=dyn_user_id,
        )
        await self._store.save(session)
        self._session = session
        self._loaded = True
        return token

    def native_headers(self, token: str, *, sha1_password: SecretStr) -> dict[str, str]:
        dyn_user_id = self._session.dyn_user_id if self._session is not None else ""
        return {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Media-Type": "application/json",
            "Sessiontoken": token,
            "Apiid": "ANDROID_V10.11",
            "Sha1password": sha1_password.get_secret_value(),
            "Dyn_user_id": dyn_user_id,
            "Os": "Android",
            "Osversion": "34",
            "Appversion": "10.11.0",
            "Iscognito": "true",
            "Deviceversion": "samsung",
            "Devicemodel": "SM-S928B",
            "Network": "Unavailable",
            "User-Agent": "okhttp/4.12.0",
        }


def _claims(token: str) -> tuple[datetime, str]:
    """Decode non-authoritative JWT metadata; Cognito remains the token authority."""
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded))
        expiry = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
        user_id = str(payload.get("custom:AtgId", ""))
    except (binascii.Error, IndexError, KeyError, OSError, TypeError, ValueError) as exc:
        raise RetailerProtocolError("Woolworths returned an invalid identity token") from exc
    return expiry, user_id
