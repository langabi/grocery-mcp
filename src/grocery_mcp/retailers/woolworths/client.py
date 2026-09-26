from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import SecretStr

from grocery_mcp.retailers.base import (
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerUnavailableError,
)
from grocery_mcp.retailers.woolworths.auth import WoolworthsAuth


@dataclass(frozen=True, slots=True)
class WoolworthsConfig:
    cognito_url: str = "https://cognito-idp.eu-west-1.amazonaws.com/"
    cognito_client_id: str = "kncqim4s1upf5ktp7lt6j3cvr"
    constructor_base: str = "https://wpkmgeuco-zone.cnstrc.com"
    constructor_key: str = "key_tw9hKe0fkfgEf36D"
    wfs_base: str = "https://wfs-appserver.wigroup.co/wfs/app/v4"
    sha1_password: str = "42058a7d46a6191bd3a5e0e792e1b1d5cc7638aa"
    place_id: str | None = None
    store_id: str | None = None
    timeout_seconds: float = 15.0


class WoolworthsClient:
    def __init__(
        self,
        http: httpx.AsyncClient,
        auth: WoolworthsAuth,
        *,
        config: WoolworthsConfig | None = None,
    ) -> None:
        self.http = http
        self.auth = auth
        self.config = config or WoolworthsConfig()
        self._client_id = str(uuid4())

    async def search(self, query: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "key": self.config.constructor_key,
            "num_results_per_page": str(limit),
            "c": "grocery-mcp-0.1",
            "i": self._client_id,
            "s": "1",
        }
        payload = await self._public_get(f"/search/{quote(query, safe='')}", params=params)
        response = _mapping(payload, "Constructor response")
        response_data = _mapping(response.get("response"), "Constructor response.response")
        results = _list(response_data.get("results"), "Constructor response.results")
        if results:
            return [_mapping(item, "Constructor product") for item in results[:limit]]

        autocomplete = await self._public_get(
            f"/autocomplete/{quote(query, safe='')}",
            params={"key": self.config.constructor_key, "num_results_Products": str(limit)},
        )
        sections = _mapping(_mapping(autocomplete, "autocomplete").get("sections"), "sections")
        products = _list(sections.get("Products"), "sections.Products")
        return [_mapping(item, "Constructor product") for item in products[:limit]]

    async def product_detail(self, sku: str) -> dict[str, Any]:
        self._require_delivery_context()
        payload = await self.wfs(
            "GET",
            f"/productsV2/{quote(sku, safe='')}",
            params={"sku": sku, "deliveryType": "OnDemand"},
        )
        root = _mapping(payload, "product response")
        candidate = root.get("product", root.get("data", root))
        return _mapping(candidate, "product")

    async def get_cart(self) -> dict[str, Any]:
        self._require_delivery_context()
        return _mapping(await self.wfs("GET", "/cartV2"), "cart response")

    async def get_saved_locations(self) -> list[dict[str, Any]]:
        """Return the minimum operator-visible fields needed to select a delivery context."""
        try:
            payload = await self.wfs("GET", "/cart/checkout/savedAddresses")
            locations = _saved_locations(payload)
        except RetailerProtocolError:
            locations = []
        if not locations:
            locations = _saved_locations(await self.wfs("GET", "/addresses"))
        return locations

    async def add_item(self, sku: str, quantity: int) -> None:
        self._require_delivery_context()
        await self.wfs(
            "POST",
            "/cart/OnDemand/itemV2",
            json=[
                {
                    "catalogRefId": sku,
                    "productId": sku,
                    "quantity": quantity,
                    "substitutionSelection": "SHOPPER_CHOICE",
                }
            ],
        )

    async def set_quantity(self, commerce_id: str, quantity: int) -> None:
        self._require_delivery_context()
        await self.wfs(
            "PUT", f"/cartV2/item/{quote(commerce_id, safe='')}", json={"quantity": quantity}
        )

    async def remove_item(self, commerce_id: str) -> None:
        self._require_delivery_context()
        await self.wfs("DELETE", "/cartV2/item", json={"commerceId": commerce_id})

    async def _public_get(self, path: str, *, params: dict[str, str]) -> Any:
        return await self._request(
            "GET",
            f"{self.config.constructor_base}{path}",
            params=params,
            headers={"Accept": "application/json"},
        )

    async def wfs(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: Any = None,
    ) -> Any:
        token = await self.auth.get_token()
        headers = self.auth.native_headers(
            token, sha1_password=SecretStr(self.config.sha1_password)
        )
        headers.update(
            {"Placeid": self.config.place_id or "", "Storeid": self.config.store_id or ""}
        )
        response = await self._send(method, f"{self.config.wfs_base}{path}", params, json, headers)
        if response.status_code == 401:
            token = await self.auth.refresh()
            headers = self.auth.native_headers(
                token, sha1_password=SecretStr(self.config.sha1_password)
            )
            headers.update(
                {"Placeid": self.config.place_id or "", "Storeid": self.config.store_id or ""}
            )
            response = await self._send(
                method, f"{self.config.wfs_base}{path}", params, json, headers
            )
        return self._decode(response, authentication=True)

    def _require_delivery_context(self) -> None:
        if not self.config.place_id or not self.config.store_id:
            raise RetailerProtocolError("Woolworths delivery context is not configured")

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        response = await self._send(method, url, params, None, headers)
        return self._decode(response, authentication=False)

    async def _send(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None,
        json: Any,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        try:
            return await self.http.request(
                method,
                url,
                params=params,
                json=json,
                headers=headers,
                timeout=self.config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise RetailerUnavailableError("Woolworths request timed out") from exc
        except httpx.HTTPError as exc:
            raise RetailerUnavailableError("Woolworths is unavailable") from exc

    @staticmethod
    def _decode(response: httpx.Response, *, authentication: bool) -> Any:
        if response.status_code in {401, 403} and authentication:
            raise RetailerAuthenticationError("Woolworths session was rejected")
        if response.status_code == 429 or response.status_code >= 500:
            raise RetailerUnavailableError("Woolworths is unavailable")
        if not 200 <= response.status_code < 300:
            raise RetailerProtocolError("Woolworths returned an unexpected status")
        try:
            return response.json()
        except ValueError as exc:
            raise RetailerProtocolError("Woolworths returned an invalid response") from exc


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RetailerProtocolError(f"{label} had an unexpected shape")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise RetailerProtocolError(f"{label} had an unexpected shape")
    return value


def _saved_locations(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        raw_locations = payload
    elif isinstance(payload, dict):
        raw_locations = next(
            (
                value
                for key in ("savedAddresses", "addresses", "items", "data")
                if isinstance((value := payload.get(key)), list)
            ),
            [],
        )
    else:
        raise RetailerProtocolError("Woolworths address response had an unexpected shape")

    locations = []
    for raw in raw_locations:
        if not isinstance(raw, dict):
            continue
        place_id = raw.get("placeId") or raw.get("placesId")
        store_id = raw.get("storeId") or raw.get("store_id")
        if not isinstance(place_id, str) or not isinstance(store_id, str):
            continue
        locations.append(
            {
                "nickname": _optional_string(
                    raw.get("nickname") or raw.get("name") or raw.get("shipToAddressName")
                ),
                "place_id": place_id,
                "store_id": store_id,
                "is_default": bool(
                    raw.get("defaultAddress") or raw.get("isDefault") or raw.get("default")
                ),
            }
        )
    return locations


def _optional_string(value: Any) -> str:
    return value if isinstance(value, str) else ""
