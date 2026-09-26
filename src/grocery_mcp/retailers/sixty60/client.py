from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from grocery_mcp.http_debug import event_hooks
from grocery_mcp.retailers.base import (
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerUnavailableError,
)


@dataclass(frozen=True, slots=True)
class Sixty60Config:
    latitude: float | None = -33.9249
    longitude: float | None = 18.4241
    timeout_seconds: float = 15.0
    app_version: str = "iPadOS 2.0.99"
    app_build: str = "unknown"
    device_id: str = ""
    dsl_api_key: str = ""
    auth_api_key: str = ""
    profile_api_token: str = ""
    bff_base_url: str = "https://dc-app-backend-for-frontend.sixty60.co.za"
    dsl_base_url: str = "https://api.shopritegroup.co.za/dsl/brands/checkers/countries/ZA"
    auth_base_url: str = "https://auth.sixty60.co.za"
    catalog_base_url: str = "https://catalog.sixty60.co.za"
    orders_base_url: str = "https://orders-api.sixty60.co.za"


class Sixty60Client:
    """Strict async transport for the undocumented Sixty60 APIs.

    Error messages intentionally contain neither response bodies nor request headers.
    """

    def __init__(
        self,
        config: Sixty60Config,
        http: httpx.AsyncClient | None = None,
        *,
        debug_http: bool = False,
    ) -> None:
        self.config = config
        self._owns_http = http is None
        self.http = http or httpx.AsyncClient(
            timeout=config.timeout_seconds, event_hooks=event_hooks(debug_http)
        )
        self.device_id = config.device_id or str(uuid.uuid4())

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self.http.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json,
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException:
            # Some upstream URLs contain credentials; never retain the transport
            # exception as a printable cause.
            raise RetailerUnavailableError("Sixty60 request timed out") from None
        except httpx.TransportError:
            raise RetailerUnavailableError("Sixty60 network request failed") from None

        if response.status_code in {401, 403}:
            raise RetailerAuthenticationError("Sixty60 authentication was rejected")
        if response.status_code >= 500:
            raise RetailerUnavailableError(f"Sixty60 service returned HTTP {response.status_code}")
        if not response.is_success:
            raise RetailerProtocolError(f"Sixty60 request returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            # JSON decoder messages may quote response content.
            raise RetailerProtocolError("Sixty60 returned invalid JSON") from None
        if not isinstance(payload, dict):
            raise RetailerProtocolError("Sixty60 returned an unexpected response shape")
        return payload

    def headers(
        self,
        token: str,
        *,
        phone: str | None = None,
        store_ids: list[str] | None = None,
        user_id: str | None = None,
        customer_id: str | None = None,
        email: str | None = None,
    ) -> dict[str, str]:
        stores = store_ids or []
        result = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "device-id": self.device_id,
            "channel": "super-app",
            "app-version": self.config.app_version,
            "appversion": self.config.app_build,
            "storeids": ",".join(stores),
            "istio-storeIds": ",".join(stores),
        }
        optional = {
            "mobileNumber": phone,
            "UserId": user_id,
            "customer-id": customer_id,
            "email": email,
        }
        result.update({key: value for key, value in optional.items() if value})
        return result
