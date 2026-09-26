from __future__ import annotations

from grocery_mcp.domain.models import Retailer, RetailerCart, RetailerHealth, RetailerProduct
from grocery_mcp.retailers.base import (
    RetailerAdapter,
    RetailerError,
    RetailerProtocolError,
    RetailerWriteDisabledError,
)
from grocery_mcp.retailers.woolworths.client import WoolworthsClient
from grocery_mcp.retailers.woolworths.mapper import (
    map_cart,
    map_constructor_product,
    map_product_detail,
)


class WoolworthsAdapter(RetailerAdapter):
    def __init__(self, client: WoolworthsClient, *, write_enabled: bool = False) -> None:
        self.client = client
        self.write_enabled = write_enabled

    async def search_products(self, query: str, limit: int = 20) -> list[RetailerProduct]:
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        return [map_constructor_product(item) for item in await self.client.search(query, limit)]

    async def get_product(self, retailer_product_id: str) -> RetailerProduct:
        product_id = _product_id(retailer_product_id)
        return map_product_detail(product_id, await self.client.product_detail(product_id))

    async def get_cart(self) -> RetailerCart:
        return map_cart(await self.client.get_cart())

    async def add_to_cart(self, retailer_product_id: str, quantity: int) -> RetailerCart:
        self._require_write()
        product_id = _product_id(retailer_product_id)
        _positive_quantity(quantity)
        before = await self.get_cart()
        existing = _line(before, product_id)
        expected = quantity
        if existing is not None:
            expected += existing.quantity
            if existing.retailer_line_id is None:
                raise RetailerProtocolError("Woolworths cart line identifier was missing")
            await self.client.set_quantity(existing.retailer_line_id, expected)
        else:
            await self.client.add_item(product_id, quantity)
        return await self._verified_quantity(product_id, expected)

    async def remove_from_cart(self, retailer_product_id: str) -> RetailerCart:
        self._require_write()
        product_id = _product_id(retailer_product_id)
        before = await self.get_cart()
        existing = _line(before, product_id)
        if existing is None:
            return before
        if existing.retailer_line_id is None:
            raise RetailerProtocolError("Woolworths cart line identifier was missing")
        await self.client.remove_item(existing.retailer_line_id)
        return await self._verified_quantity(product_id, 0)

    async def set_quantity(self, retailer_product_id: str, quantity: int) -> RetailerCart:
        self._require_write()
        product_id = _product_id(retailer_product_id)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise ValueError("quantity must be a non-negative integer")
        before = await self.get_cart()
        existing = _line(before, product_id)
        if quantity == 0:
            if existing is None:
                return before
            if existing.retailer_line_id is None:
                raise RetailerProtocolError("Woolworths cart line identifier was missing")
            await self.client.remove_item(existing.retailer_line_id)
        elif existing is None:
            await self.client.add_item(product_id, quantity)
        else:
            if existing.retailer_line_id is None:
                raise RetailerProtocolError("Woolworths cart line identifier was missing")
            await self.client.set_quantity(existing.retailer_line_id, quantity)
        return await self._verified_quantity(product_id, quantity)

    async def health_check(self) -> RetailerHealth:
        catalogue = False
        authentication = False
        cart_read = False
        details: list[str] = []
        try:
            await self.search_products("milk", limit=1)
            catalogue = True
        except RetailerError as exc:
            details.append(type(exc).__name__)
        try:
            if await self.client.auth.has_persisted_session() or self.client.auth.can_authenticate:
                authentication = True
                await self.get_cart()
                cart_read = True
        except RetailerError as exc:
            authentication = False
            details.append(type(exc).__name__)
        return RetailerHealth(
            retailer=Retailer.WOOLWORTHS,
            catalogue=catalogue,
            authentication=authentication,
            cart_read=cart_read,
            cart_write=cart_read and self.write_enabled,
            detail=", ".join(details) or None,
        )

    async def _verified_quantity(self, product_id: str, expected: int) -> RetailerCart:
        cart = await self.get_cart()
        line = _line(cart, product_id)
        actual = 0 if line is None else line.quantity
        if actual != expected:
            raise RetailerProtocolError("Woolworths cart write could not be verified")
        return cart

    def _require_write(self) -> None:
        if not self.write_enabled:
            raise RetailerWriteDisabledError("Woolworths cart writes are disabled")


def _line(cart: RetailerCart, product_id: str):
    matches = [line for line in cart.items if line.retailer_product_id == product_id]
    if len(matches) > 1:
        raise RetailerProtocolError("Woolworths cart contains ambiguous product lines")
    return matches[0] if matches else None


def _product_id(value: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError("retailer_product_id must not be empty")
    return value


def _positive_quantity(value: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("quantity must be a positive integer")
