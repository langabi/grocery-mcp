from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from grocery_mcp.domain.models import CartLine, ProductSize, Retailer, RetailerCart, RetailerProduct
from grocery_mcp.domain.products import extract_product_size
from grocery_mcp.retailers.base import RetailerProtocolError

ON_DEMAND = "OnDemand"


def map_constructor_product(item: dict[str, Any]) -> RetailerProduct:
    data = item.get("data")
    if not isinstance(data, dict):
        raise RetailerProtocolError("Woolworths product had an unexpected shape")
    sku = data.get("id", item.get("id", data.get("catalogRefId")))
    name = item.get("value", data.get("name", data.get("productDisplayName")))
    price = _first(data, "price", "p10", "salePrice", "list_price")
    if not sku or not isinstance(name, str) or not name.strip() or price is None:
        raise RetailerProtocolError("Woolworths product was missing required fields")
    path = data.get("url")
    product_url = None
    if isinstance(path, str) and path:
        product_url = (
            path if path.startswith("http") else f"https://www.woolworths.co.za/{path.lstrip('/')}"
        )
    if product_url is None:
        product_url = f"https://www.woolworths.co.za/prod/_/A-{sku}"
    return RetailerProduct(
        retailer=Retailer.WOOLWORTHS,
        retailer_product_id=str(sku),
        sku=str(sku),
        name=name.strip(),
        brand=_optional_text(data.get("brand")),
        size=_parse_size(name),
        price=_money(price),
        in_stock=_optional_bool(data.get("in_stock", data.get("isInStock"))),
        image_url=_optional_text(data.get("image_url", data.get("imageUrl"))),
        product_url=product_url,
        raw=item,
    )


def map_product_detail(sku: str, data: dict[str, Any]) -> RetailerProduct:
    name = _first(data, "productDisplayName", "displayName", "name")
    price = _first(data, "price", "salePrice")
    if not isinstance(name, str) or not name.strip() or price is None:
        raise RetailerProtocolError("Woolworths product detail was missing required fields")
    return RetailerProduct(
        retailer=Retailer.WOOLWORTHS,
        retailer_product_id=sku,
        sku=sku,
        name=name.strip(),
        brand=_optional_text(data.get("brand")),
        size=_parse_size(name),
        price=_money(price),
        in_stock=_optional_bool(data.get("inStock", data.get("isInStock"))),
        image_url=_optional_text(data.get("imageUrl")),
        product_url=f"https://www.woolworths.co.za/prod/_/A-{sku}",
        raw=data,
    )


def map_cart(payload: dict[str, Any]) -> RetailerCart:
    carts = payload.get("data")
    if not isinstance(carts, list):
        raise RetailerProtocolError("Woolworths cart had an unexpected shape")
    if not carts:
        return RetailerCart(retailer=Retailer.WOOLWORTHS, cart_id="woolworths-ondemand", total=0)
    cart = _select_cart(carts)
    item_groups = cart.get("items", {})
    if not isinstance(item_groups, dict):
        raise RetailerProtocolError("Woolworths cart items had an unexpected shape")
    lines: list[CartLine] = []
    for group in item_groups.values():
        if not isinstance(group, list):
            raise RetailerProtocolError("Woolworths cart item group had an unexpected shape")
        for raw_line in group:
            if not isinstance(raw_line, dict) or not isinstance(
                raw_line.get("commerceItemInfo"), dict
            ):
                raise RetailerProtocolError("Woolworths cart line had an unexpected shape")
            info = raw_line["commerceItemInfo"]
            commerce_id = info.get("id")
            sku = info.get("catalogRefId")
            name = info.get("productDisplayName")
            quantity = info.get("quantity")
            price = _first(info, "price", "salePrice")
            if not commerce_id or not sku or not isinstance(name, str) or price is None:
                raise RetailerProtocolError("Woolworths cart line was missing required fields")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise RetailerProtocolError("Woolworths cart line had an invalid quantity")
            lines.append(
                CartLine(
                    retailer_product_id=str(sku),
                    retailer_line_id=str(commerce_id),
                    name=name,
                    quantity=quantity,
                    unit_price=_money(price),
                )
            )
    summary = cart.get("orderSummary")
    if not isinstance(summary, dict) or summary.get("total") is None:
        raise RetailerProtocolError("Woolworths cart total was missing")
    cart_id = _first(cart, "id", "cartId", "orderId") or "woolworths-ondemand"
    return RetailerCart(
        retailer=Retailer.WOOLWORTHS,
        cart_id=str(cart_id),
        items=lines,
        total=_money(summary["total"]),
        store_id=_optional_text(_first(cart, "storeId", "store_id")),
    )


def _select_cart(carts: list[Any]) -> dict[str, Any]:
    """Identify the on-demand cart exactly; an ambiguous account must not be guessed at."""
    typed = [
        cart
        for cart in carts
        if isinstance(cart, dict) and str(cart.get("deliveryType", "")).lower() == ON_DEMAND.lower()
    ]
    candidates = typed or [cart for cart in carts if isinstance(cart, dict)]
    if not candidates:
        raise RetailerProtocolError("Woolworths cart had an unexpected shape")
    if len(candidates) > 1:
        raise RetailerProtocolError("Woolworths returned more than one cart for this context")
    return candidates[0]


def _money(value: Any) -> Decimal:
    try:
        amount = Decimal(str(value).replace("R", "").replace(" ", "").replace(",", ""))
    except (InvalidOperation, ValueError) as exc:
        raise RetailerProtocolError("Woolworths returned an invalid price") from exc
    if not amount.is_finite() or amount < 0:
        raise RetailerProtocolError("Woolworths returned an invalid price")
    return amount


def _parse_size(name: str) -> ProductSize | None:
    return extract_product_size(name)


def _first(data: dict[str, Any], *keys: str) -> Any:
    return next((data[key] for key in keys if data.get(key) is not None), None)


def _optional_text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
