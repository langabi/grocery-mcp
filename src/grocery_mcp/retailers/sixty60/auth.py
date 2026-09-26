from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from grocery_mcp.domain.models import Retailer
from grocery_mcp.retailers.base import RetailerProtocolError


class OtpChallenge(BaseModel):
    """Short-lived state returned to the trusted admin login flow."""

    model_config = ConfigDict(extra="forbid")

    phone_e164: str
    customer_id: str
    reference: str
    bff_token: str = Field(repr=False)


class Sixty60Session(BaseModel):
    """Persisted retailer session. Callers must encrypt this at rest."""

    model_config = ConfigDict(extra="forbid")

    phone_e164: str
    customer_id: str
    user_id: str
    email: str
    access_token: str = Field(repr=False)
    refresh_token: str | None = Field(default=None, repr=False)
    store_ids: list[str] = Field(default_factory=list)
    cart_id: str | None = None


class SessionStore(Protocol):
    async def load(self) -> Sixty60Session | None: ...

    async def save(self, session: Sixty60Session) -> None: ...

    async def delete(self) -> None: ...


class RetailerStateBackend(Protocol):
    async def load_session(self, retailer: Retailer | str) -> dict[str, object] | None: ...

    async def save_session(self, retailer: Retailer | str, payload: dict[str, object]) -> None: ...

    async def clear_session(self, retailer: Retailer | str) -> None: ...


class PersistentSixty60SessionStore:
    """Typed bridge to the application's encrypted retailer-state backend."""

    def __init__(self, backend: RetailerStateBackend) -> None:
        self._backend = backend

    async def load(self) -> Sixty60Session | None:
        payload = await self._backend.load_session(Retailer.SIXTY60)
        if payload is None:
            return None
        try:
            return Sixty60Session.model_validate(payload)
        except Exception:
            raise RetailerProtocolError("Stored Sixty60 session was invalid") from None

    async def save(self, session: Sixty60Session) -> None:
        await self._backend.save_session(Retailer.SIXTY60, session.model_dump(mode="json"))

    async def delete(self) -> None:
        await self._backend.clear_session(Retailer.SIXTY60)


class MemorySessionStore:
    """Small test/development store; production should inject encrypted persistence."""

    def __init__(self, session: Sixty60Session | None = None) -> None:
        self._session = session.model_copy(deep=True) if session else None

    async def load(self) -> Sixty60Session | None:
        return self._session.model_copy(deep=True) if self._session else None

    async def save(self, session: Sixty60Session) -> None:
        self._session = session.model_copy(deep=True)

    async def delete(self) -> None:
        self._session = None
