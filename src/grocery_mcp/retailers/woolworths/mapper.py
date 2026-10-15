from __future__ import annotations

from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from grocery_mcp.domain.models import (
    CartLine,
    OrderLine,
    ProductSize,
    Retailer,
    RetailerCart,
    RetailerOrder,
    RetailerProduct,
)
from grocery_mcp.domain.products import extract_product_size
from grocery_mcp.retailers.base import RetailerProtocolError

ON_DEMAND = "OnDemand"
SOUTH_AFRICA = ZoneInfo("Africa/Johannesburg")


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
    """Map a productsV2 payload.

    The payload has no field with a known stock meaning, so in_stock is left unknown.
    """
    name = data.get("productName")
    price = data.get("price")
    if not isinstance(name, str) or not name.strip() or price is None:
        raise RetailerProtocolError("Woolworths product detail was missing required fields")
    return RetailerProduct(
        retailer=Retailer.WOOLWORTHS,
        retailer_product_id=sku,
        sku=sku,
        name=name.strip(),
        brand=_optional_text(data.get("brandText")),
        size=_parse_size(name),
        price=_money(price),
        image_url=_optional_text(data.get("externalImageRefV2")),
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
            price = _unit_price(raw_line)
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
    cart_id = _first(cart, "basketId", "id", "cartId") or "woolworths-ondemand"
    return RetailerCart(
        retailer=Retailer.WOOLWORTHS,
        cart_id=str(cart_id),
        items=lines,
        total=_money(summary["total"]),
        store_id=_optional_text(_first(cart, "storeId", "store_id")),
    )


def map_orders(payload: Any) -> list[RetailerOrder]:
    """Map the order list, which carries per-order totals and counts but no line items."""
    return [map_order(item) for item in _order_collection(payload)]


def map_order(raw: dict[str, Any]) -> RetailerOrder:
    order_id = raw.get("orderId")
    if not isinstance(order_id, str) or not order_id.strip():
        raise RetailerProtocolError("Woolworths order history entry was missing an order ID")
    status = raw.get("state") or "unknown"
    if not isinstance(status, str):
        raise RetailerProtocolError("Woolworths order history entry had an invalid status")
    raw_total = raw.get("total")
    raw_count = raw.get("totalItemsCount")
    return RetailerOrder(
        retailer=Retailer.WOOLWORTHS,
        order_id=order_id.strip(),
        status=status.strip() or "unknown",
        created_at=_submitted_at(raw.get("submittedDate")),
        total=_money(raw_total) if raw_total is not None else None,
        item_count=(
            raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else None
        ),
    )


def map_order_lines(payload: Any) -> list[OrderLine]:
    """Map the line items of a single-order payload, whose items are grouped by business unit."""
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), dict):
        raise RetailerProtocolError("Woolworths order detail had an unexpected shape")
    lines: list[OrderLine] = []
    for group in payload["items"].values():
        if not isinstance(group, list):
            raise RetailerProtocolError("Woolworths order item group had an unexpected shape")
        for wrapper in group:
            if not isinstance(wrapper, dict) or not isinstance(
                wrapper.get("commerceItemInfo"), dict
            ):
                raise RetailerProtocolError("Woolworths order line had an unexpected shape")
            info = wrapper["commerceItemInfo"]
            product_id = info.get("catalogRefId")
            name = info.get("productDisplayName")
            if not isinstance(name, str) or not name.strip():
                if not isinstance(product_id, str):
                    raise RetailerProtocolError("Woolworths order line was missing product details")
                name = f"Product name unavailable ({product_id})"
            quantity = info.get("quantity")
            if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
                raise RetailerProtocolError("Woolworths order line had an invalid quantity")
            price_info = wrapper.get("priceInfo")
            raw_line_total = price_info.get("amount") if isinstance(price_info, dict) else None
            unit_price = _unit_price(wrapper)
            lines.append(
                OrderLine(
                    retailer_product_id=product_id if isinstance(product_id, str) else None,
                    name=name.strip(),
                    quantity=quantity,
                    unit_price=_money(unit_price) if unit_price is not None else None,
                    line_total=_money(raw_line_total) if raw_line_total is not None else None,
                )
            )
    return lines


def _order_collection(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RetailerProtocolError("Woolworths order history had an unexpected shape")
    upcoming = payload.get("upcomingOrders", [])
    past = payload.get("pastOrders", [])
    if not isinstance(upcoming, list) or not isinstance(past, list):
        raise RetailerProtocolError("Woolworths order history had an unexpected shape")
    raw_orders = upcoming + past
    if not all(isinstance(item, dict) for item in raw_orders):
        raise RetailerProtocolError("Woolworths order history had an unexpected shape")
    return raw_orders


def _unit_price(line: dict[str, Any]) -> Any:
    """Return the shelf price of one unit.

    priceInfo.amount is the line total after discounts, and salePrice is 0.0 for an item that
    is not on sale, so neither is a unit price.
    """
    price_info = line.get("priceInfo")
    return price_info.get("listPrice") if isinstance(price_info, dict) else None


def _submitted_at(value: Any) -> datetime | None:
    """Parse an order's submittedDate, a DD/MM/YYYY HH:MM:SS South African local time.

    The order list reports every time as 12:00:00, so only the date is meaningful there.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise RetailerProtocolError("Woolworths order had an invalid timestamp")
    try:
        parsed = datetime.strptime(value.strip(), "%d/%m/%Y %H:%M:%S")
    except ValueError:
        raise RetailerProtocolError("Woolworths order had an invalid timestamp") from None
    return parsed.replace(tzinfo=SOUTH_AFRICA)


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
