from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

from grocery_mcp.domain.models import Retailer, RetailerCart, RetailerHealth, RetailerProduct
from grocery_mcp.retailers.base import (
    RetailerAdapter,
    RetailerAuthenticationError,
    RetailerProtocolError,
    RetailerWriteDisabledError,
)
from grocery_mcp.retailers.sixty60.auth import OtpChallenge, SessionStore, Sixty60Session
from grocery_mcp.retailers.sixty60.client import Sixty60Client
from grocery_mcp.retailers.sixty60.mapper import cart_from_payload, product_from_payload

SERVICE_OPTION = "sixty-min-delivery"


@dataclass(slots=True)
class _CartBundle:
    selected: dict[str, Any]
    carts: list[dict[str, Any]]
    store_contexts: list[dict[str, Any]]


class Sixty60Adapter(RetailerAdapter):
    def __init__(
        self,
        client: Sixty60Client,
        session_store: SessionStore,
        *,
        write_enabled: bool = False,
    ) -> None:
        self.client = client
        self.session_store = session_store
        self.write_enabled = write_enabled
        self._mutation_lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.close()

    async def start_login(self, phone: str) -> OtpChallenge:
        """Start the trusted admin OTP flow. This sends an SMS."""
        self._require_auth_config()
        phone_e164 = _normalize_phone(phone)
        bff_token = await self._bff_token()
        verified = await self.client.request_json(
            "GET",
            f"{self.client.config.dsl_base_url}/users/verify",
            headers={
                **self.client.headers(bff_token, phone=phone_e164),
                "x-api-key": self.client.config.dsl_api_key,
            },
        )
        customer_id = _nested_string(verified, "response", "uid")
        if not customer_id:
            raise RetailerAuthenticationError("Sixty60 could not identify that account")
        requested = await self.client.request_json(
            "GET",
            f"{self.client.config.dsl_base_url}/users/loginbymobile",
            params={"mobileNumber": phone_e164},
            headers={
                **self.client.headers(bff_token, phone=phone_e164, customer_id=customer_id),
                "x-api-key": self.client.config.auth_api_key,
            },
        )
        reference = _nested_string(requested, "response", "reference")
        if not reference:
            raise RetailerProtocolError("Sixty60 did not return an OTP reference")
        return OtpChallenge(
            phone_e164=phone_e164,
            customer_id=customer_id,
            reference=reference,
            bff_token=bff_token,
        )

    async def complete_login(self, challenge: OtpChallenge, otp: str) -> Sixty60Session:
        """Complete OTP login and persist the resulting account context."""
        self._require_auth_config()
        if not otp.isdigit() or not 4 <= len(otp) <= 8:
            raise ValueError("OTP must contain 4 to 8 digits")
        verified = await self.client.request_json(
            "POST",
            f"{self.client.config.dsl_base_url}/otp/loginbymobile/verify",
            headers={
                **self.client.headers(
                    challenge.bff_token,
                    phone=challenge.phone_e164,
                    customer_id=challenge.customer_id,
                ),
                "x-api-key": self.client.config.auth_api_key,
            },
            json={
                "target": {
                    "type": "SMS",
                    "identifier": challenge.phone_e164,
                    "reference": challenge.reference,
                },
                "otp": otp,
            },
        )
        access_token = _nested_string(verified, "response", "accessToken")
        refresh_token = _nested_string(verified, "response", "refreshToken")
        if not access_token:
            raise RetailerAuthenticationError("Sixty60 did not establish a session")
        profile = await self.client.request_json(
            "GET",
            (
                f"{self.client.config.auth_base_url}/customers/"
                f"{challenge.customer_id}/customer-profile/v2/{access_token}"
            ),
            headers={
                **self.client.headers(access_token, phone=challenge.phone_e164),
                "Authorization": f"Bearer {self.client.config.profile_api_token}",
            },
        )
        profile_data = profile.get("userProfile")
        if not isinstance(profile_data, dict):
            raise RetailerProtocolError("Sixty60 returned an invalid profile response")
        user_id = profile_data.get("id") or profile_data.get("identifier")
        email = profile_data.get("email")
        if not isinstance(user_id, str) or not user_id or not isinstance(email, str) or not email:
            raise RetailerProtocolError("Sixty60 profile is missing required account fields")
        partial = Sixty60Session(
            phone_e164=challenge.phone_e164,
            customer_id=challenge.customer_id,
            user_id=user_id,
            email=email,
            access_token=access_token,
            refresh_token=refresh_token,
        )
        contexts = await self._store_contexts(partial)
        partial.store_ids = _store_ids(contexts)
        await self.session_store.save(partial)
        return partial.model_copy(deep=True)

    # Names suitable for admin-command routing.
    start_otp = start_login
    complete_otp = complete_login

    async def pin_cart(self, cart_id: str) -> None:
        if not cart_id.strip():
            raise ValueError("cart_id must not be empty")
        session = await self._session()
        session.cart_id = cart_id
        await self.session_store.save(session)

    async def search_products(self, query: str, limit: int = 20) -> list[RetailerProduct]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        session = await self.session_store.load()
        token = session.access_token if session else await self._bff_token()
        contexts = await self._store_contexts(session, token=token)
        store_ids = _store_ids(contexts)
        headers = self._session_headers(session, token, store_ids)
        payload = await self.client.request_json(
            "POST",
            f"{self.client.config.catalog_base_url}/api/v3/products/product-list-page",
            params={
                "isCarousel": "true",
                "includePromotions": "true",
                "promotionChannel": "sixty60",
                "t": str(int(time.time() * 1000)),
            },
            headers=headers,
            json={
                "filter": {
                    "productListSource": {"search": query},
                    "paginationOptions": {"page": 0, "pageSize": limit},
                    "filterOptions": {
                        "dealsOnly": False,
                        "brandOptions": [],
                        "departmentOptions": [],
                        "facetOptions": [],
                        "serviceOptions": [],
                        "filterIds": [],
                    },
                    "showNotRangedProducts": False,
                },
                "userContext": {
                    "storeContexts": contexts,
                    **({"userId": session.user_id} if session else {}),
                },
            },
        )
        raw_products = payload.get("products")
        if not isinstance(raw_products, list):
            raise RetailerProtocolError("Sixty60 search response has no product list")
        products = [product_from_payload(item) for item in raw_products if isinstance(item, dict)]
        return products[:limit]

    async def get_product(self, retailer_product_id: str) -> RetailerProduct:
        if not retailer_product_id.strip():
            raise ValueError("retailer_product_id must not be empty")
        return await self._lookup_product(retailer_product_id)

    async def get_cart(self) -> RetailerCart:
        session = await self._session()
        bundle = await self._read_cart_bundle(session, require_pinned=False)
        return cart_from_payload(bundle.selected)

    async def add_to_cart(self, retailer_product_id: str, quantity: int) -> RetailerCart:
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        async with self._mutation_lock:
            session = await self._write_session()
            bundle = await self._read_cart_bundle(session, require_pinned=True)
            current = _line_quantity(bundle.selected, retailer_product_id)
            return await self._replace_quantity(
                session, bundle, retailer_product_id, current + quantity
            )

    async def remove_from_cart(self, retailer_product_id: str) -> RetailerCart:
        async with self._mutation_lock:
            session = await self._write_session()
            bundle = await self._read_cart_bundle(session, require_pinned=True)
            if _find_line(bundle.selected, retailer_product_id) is None:
                raise RetailerProtocolError("Requested product is not in the selected cart")
            return await self._replace_quantity(session, bundle, retailer_product_id, 0)

    async def set_quantity(self, retailer_product_id: str, quantity: int) -> RetailerCart:
        if quantity < 0:
            raise ValueError("quantity must not be negative")
        async with self._mutation_lock:
            session = await self._write_session()
            bundle = await self._read_cart_bundle(session, require_pinned=True)
            if quantity == 0 and _find_line(bundle.selected, retailer_product_id) is None:
                raise RetailerProtocolError("Requested product is not in the selected cart")
            return await self._replace_quantity(session, bundle, retailer_product_id, quantity)

    async def health_check(self) -> RetailerHealth:
        catalogue = False
        detail: str | None = None
        try:
            await self._bff_token()
            catalogue = True
        except Exception as exc:  # Health converts typed failures into capability state.
            detail = type(exc).__name__
        session = await self.session_store.load()
        authenticated = session is not None
        cart_read = False
        if session:
            try:
                await self._read_cart_bundle(session, require_pinned=False)
                cart_read = True
            except Exception as exc:
                detail = type(exc).__name__
        return RetailerHealth(
            retailer=Retailer.SIXTY60,
            catalogue=catalogue,
            authentication=authenticated,
            cart_read=cart_read,
            cart_write=cart_read and self.write_enabled and bool(session and session.cart_id),
            detail=detail,
        )

    async def _bff_token(self) -> str:
        payload = await self.client.request_json(
            "POST",
            f"{self.client.config.bff_base_url}/api/v1/token/dsl",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        token = payload.get("access_token")
        if not isinstance(token, str) or not token:
            raise RetailerProtocolError("Sixty60 bootstrap response is missing a token")
        return token

    async def _session(self) -> Sixty60Session:
        session = await self.session_store.load()
        if not session:
            raise RetailerAuthenticationError("Sixty60 login is required")
        return session

    async def _write_session(self) -> Sixty60Session:
        if not self.write_enabled:
            raise RetailerWriteDisabledError("Sixty60 cart writes are disabled")
        session = await self._session()
        if not session.cart_id:
            raise RetailerWriteDisabledError(
                "A Sixty60 cart must be explicitly pinned before writing"
            )
        return session

    async def _store_contexts(
        self, session: Sixty60Session | None, *, token: str | None = None
    ) -> list[dict[str, Any]]:
        if self.client.config.latitude is None or self.client.config.longitude is None:
            raise RetailerProtocolError("Sixty60 delivery location is not configured")
        actual_token = token or (session.access_token if session else await self._bff_token())
        payload = await self.client.request_json(
            "POST",
            f"{self.client.config.catalog_base_url}/api/v3/store-contexts",
            headers=self._session_headers(session, actual_token, []),
            json={
                "latitude": self.client.config.latitude,
                "longitude": self.client.config.longitude,
            },
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise RetailerProtocolError("Sixty60 store response has an unexpected shape")
        contexts = [dict(item) for item in items if isinstance(item, dict) and item.get("storeId")]
        if not contexts:
            raise RetailerProtocolError("No Sixty60 stores serve the configured location")
        for context in contexts:
            context.setdefault("serviceOptionIds", [SERVICE_OPTION])
            context.setdefault("brandPriority", 1)
            context.setdefault("hasCapacity", [SERVICE_OPTION])
        return contexts

    def _session_headers(
        self, session: Sixty60Session | None, token: str, store_ids: list[str]
    ) -> dict[str, str]:
        return self.client.headers(
            token,
            phone=session.phone_e164 if session else None,
            store_ids=store_ids,
            user_id=session.user_id if session else None,
            customer_id=session.customer_id if session else None,
            email=session.email if session else None,
        )

    async def _lookup_product(self, product_id: str) -> RetailerProduct:
        session = await self.session_store.load()
        token = session.access_token if session else await self._bff_token()
        contexts = await self._store_contexts(session, token=token)
        payload = await self.client.request_json(
            "POST",
            f"{self.client.config.catalog_base_url}/api/v3/products/product-list-page",
            headers=self._session_headers(session, token, _store_ids(contexts)),
            json={
                "filter": {
                    "productListSource": {"productIds": [product_id]},
                    "paginationOptions": {"page": 0, "pageSize": 1},
                    "filterOptions": {},
                    "showNotRangedProducts": False,
                },
                "userContext": {
                    "storeContexts": contexts,
                    **({"userId": session.user_id} if session else {}),
                },
            },
        )
        products = payload.get("products")
        if not isinstance(products, list):
            raise RetailerProtocolError("Sixty60 product response has an unexpected shape")
        match = next(
            (item for item in products if isinstance(item, dict) and item.get("id") == product_id),
            None,
        )
        if match is None:
            raise RetailerProtocolError("Product is unavailable in the configured store context")
        return product_from_payload(match)

    async def _read_cart_bundle(
        self, session: Sixty60Session, *, require_pinned: bool
    ) -> _CartBundle:
        contexts = await self._store_contexts(session)
        store_ids = _store_ids(contexts)
        payload = await self.client.request_json(
            "POST",
            f"{self.client.config.orders_base_url}/api/v2/carts/user",
            params={"useProductMinInfoAnnotation": "true"},
            headers=self._session_headers(session, session.access_token, store_ids),
            json={"storeContexts": contexts, "includeV2ReplacementOptions": True},
        )
        wrappers = payload.get("carts")
        if not isinstance(wrappers, list):
            raise RetailerProtocolError("Sixty60 cart response has an unexpected shape")
        carts = [
            wrapper["item"]
            for wrapper in wrappers
            if isinstance(wrapper, dict)
            and isinstance(wrapper.get("item"), dict)
            and wrapper["item"].get("id")
        ]
        if session.cart_id:
            selected = next((cart for cart in carts if cart.get("id") == session.cart_id), None)
            if selected is None:
                raise RetailerProtocolError("The pinned Sixty60 cart was not returned")
        else:
            candidates = [cart for cart in carts if cart.get("serviceOptionId") == SERVICE_OPTION]
            if require_pinned or len(candidates) != 1:
                raise RetailerProtocolError(
                    "Sixty60 cart selection is ambiguous; pin an exact cart ID"
                )
            selected = candidates[0]
        return _CartBundle(selected=selected, carts=carts, store_contexts=contexts)

    async def _replace_quantity(
        self,
        session: Sixty60Session,
        bundle: _CartBundle,
        product_id: str,
        target: int,
    ) -> RetailerCart:
        target_lines = [dict(item) for item in _line_items(bundle.selected)]
        existing = next(
            (line for line in target_lines if _line_product_id(line) == product_id), None
        )
        if existing is not None:
            existing["quantity"] = target
            if target == 0:
                existing["status"] = "removed"
            else:
                existing.pop("status", None)
        elif target > 0:
            product = await self._lookup_product(product_id)
            if product.in_stock is False:
                raise RetailerProtocolError(
                    "Product is out of stock in the configured store context"
                )
            raw = product.raw
            store_id = product.store_id or _store_ids(bundle.store_contexts)[0]
            target_lines.append(
                {
                    "id": "",
                    "productId": product_id,
                    "storeId": store_id,
                    "price": raw.get("priceWithoutDecimal", 0),
                    "previousPrice": raw.get("oldPrice", raw.get("priceWithoutDecimal", 0)),
                    "priceFactor": raw.get("priceFactor", 100),
                    "quantity": target,
                    "serviceOptionId": bundle.selected.get("serviceOptionId", SERVICE_OPTION),
                    "replacementPreferenceId": "",
                    "specialInstruction": "",
                    "addToBasketType": "quick_add",
                    "addToBasketJourney": "main_search_results",
                    "hasAlcohol": bool(raw.get("hasAlcohol", False)),
                    "requiresOver18": bool(raw.get("requiresOver18", False)),
                    "product": None,
                }
            )
        carts_for_update = []
        for cart in bundle.carts:
            cart_id = cart.get("id")
            service_option = cart.get("serviceOptionId")
            if not isinstance(cart_id, str) or not isinstance(service_option, str):
                raise RetailerProtocolError("Sixty60 returned an incomplete cart identity")
            lines = target_lines if cart_id == bundle.selected.get("id") else _line_items(cart)
            carts_for_update.append(
                {
                    "id": cart_id,
                    "serviceOptionId": service_option,
                    "lineItems": [_update_line(line) for line in lines],
                }
            )
        address = bundle.selected.get("deliveryAddress")
        address_id = address.get("identifier") if isinstance(address, dict) else None
        if not isinstance(address_id, str) or not address_id:
            raise RetailerProtocolError("Selected Sixty60 cart has no exact delivery address")
        headers = self._session_headers(
            session, session.access_token, _store_ids(bundle.store_contexts)
        )
        await self.client.request_json(
            "POST",
            f"{self.client.config.orders_base_url}/api/v3/carts/update",
            params={"useProductMinInfoAnnotation": "true"},
            headers=headers,
            json={
                "carts": carts_for_update,
                "deliveryAddressId": address_id,
                "storeContexts": bundle.store_contexts,
            },
        )
        for cart in carts_for_update:
            await self.client.request_json(
                "POST",
                f"{self.client.config.orders_base_url}/api/v1/carts/{cart['id']}/update-promotions",
                params={
                    "include_v2_replacement_preferences": "true",
                    "useProductMinInfoAnnotation": "true",
                },
                headers=headers,
                json={"storeContexts": bundle.store_contexts},
            )
        verified = await self._read_cart_bundle(session, require_pinned=True)
        actual = _line_quantity(verified.selected, product_id)
        if actual != target:
            raise RetailerProtocolError("Sixty60 cart write could not be verified")
        return cart_from_payload(verified.selected)

    def _require_auth_config(self) -> None:
        config = self.client.config
        if not config.dsl_api_key or not config.auth_api_key or not config.profile_api_token:
            raise RetailerProtocolError("Sixty60 authentication fingerprints are not configured")


def _normalize_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if len(digits) == 11 and digits.startswith("27"):
        return f"+{digits}"
    if len(digits) == 10 and digits.startswith("0"):
        return f"+27{digits[1:]}"
    if len(digits) == 9:
        return f"+27{digits}"
    raise ValueError("phone must be a valid South African mobile number")


def _nested_string(payload: dict[str, Any], outer: str, inner: str) -> str | None:
    nested = payload.get(outer)
    value = nested.get(inner) if isinstance(nested, dict) else None
    return value if isinstance(value, str) and value else None


def _store_ids(contexts: list[dict[str, Any]]) -> list[str]:
    return [str(context["storeId"]) for context in contexts]


def _line_items(cart: dict[str, Any]) -> list[dict[str, Any]]:
    items = cart.get("lineItems", [])
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise RetailerProtocolError("Sixty60 cart line items have an unexpected shape")
    return items


def _line_product_id(line: dict[str, Any]) -> str | None:
    nested = line.get("product")
    value = line.get("productId") or (nested.get("id") if isinstance(nested, dict) else None)
    return value if isinstance(value, str) else None


def _find_line(cart: dict[str, Any], product_id: str) -> dict[str, Any] | None:
    return next((line for line in _line_items(cart) if _line_product_id(line) == product_id), None)


def _line_quantity(cart: dict[str, Any], product_id: str) -> int:
    line = _find_line(cart, product_id)
    if line is None or str(line.get("status", "")).lower() == "removed":
        return 0
    value = line.get("quantity", 0)
    if not isinstance(value, int) or value < 0:
        raise RetailerProtocolError("Sixty60 cart line has an invalid quantity")
    return value


def _update_line(line: dict[str, Any]) -> dict[str, Any]:
    """Whitelist the fields accepted by the cart rewrite endpoint."""
    fields = (
        "id",
        "productId",
        "storeId",
        "price",
        "previousPrice",
        "priceFactor",
        "quantity",
        "instruction",
        "specialInstruction",
        "specialInstructions",
        "replacementPreferenceId",
        "missionName",
        "missionType",
        "addToBasketType",
        "addToBasketJourney",
        "serviceOptionId",
        "hasAlcohol",
        "requiresOver18",
        "status",
    )
    result = {field: line[field] for field in fields if field in line}
    result["productId"] = _line_product_id(line) or ""
    result.setdefault("id", "")
    result.setdefault("quantity", 0)
    result.setdefault("price", 0)
    result.setdefault("priceFactor", 100)
    result["product"] = None
    return result
