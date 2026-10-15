from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from grocery_mcp.domain.models import (
    CartLine,
    OrderLine,
    Retailer,
    RetailerCart,
    RetailerOrder,
    RetailerProduct,
)
from grocery_mcp.domain.products import extract_product_size
from grocery_mcp.retailers.base import RetailerProtocolError


def _money(value: Any, factor: Any = None) -> Decimal:
    try:
        amount = Decimal(str(value if value is not None else 0))
        divisor = Decimal(str(factor or 100))
        return (amount / divisor).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        raise RetailerProtocolError("Sixty60 returned an invalid monetary value") from exc


def product_from_payload(payload: dict[str, Any]) -> RetailerProduct:
    product_id = payload.get("id") or payload.get("productId") or payload.get("sku")
    name = payload.get("name") or payload.get("productName") or payload.get("title")
    if not isinstance(product_id, str) or not product_id or not isinstance(name, str) or not name:
        raise RetailerProtocolError("Sixty60 product response is missing required fields")
    raw_price = payload.get("priceWithoutDecimal", payload.get("price", 0))
    factor = payload.get("priceFactor", 100 if "priceWithoutDecimal" in payload else 1)
    return RetailerProduct(
        retailer=Retailer.SIXTY60,
        retailer_product_id=product_id,
        sku=str(payload["sku"]) if payload.get("sku") is not None else None,
        name=name,
        brand=payload.get("brand") if isinstance(payload.get("brand"), str) else None,
        size=extract_product_size(name),
        price=_money(raw_price, factor),
        in_stock=_optional_bool(payload.get("isStockAvailable", payload.get("inStock"))),
        image_url=_first_string(payload, "imageUrl", "image"),
        product_url=_first_string(payload, "productUrl", "url"),
        store_id=payload.get("storeId") if isinstance(payload.get("storeId"), str) else None,
        raw=payload,
    )


def cart_from_payload(
    payload: dict[str, Any],
    products: Mapping[str, RetailerProduct] | None = None,
) -> RetailerCart:
    cart_id = payload.get("id")
    if not isinstance(cart_id, str) or not cart_id:
        raise RetailerProtocolError("Sixty60 cart response is missing a cart ID")
    lines: list[CartLine] = []
    raw_lines = payload.get("lineItems", [])
    if not isinstance(raw_lines, list):
        raise RetailerProtocolError("Sixty60 cart line items have an unexpected shape")
    for raw in raw_lines:
        if not isinstance(raw, dict) or str(raw.get("status", "")).lower() == "removed":
            continue
        nested = _cart_line_product(raw)
        product_id = raw.get("productId") or nested.get("id")
        if not isinstance(product_id, str) or not product_id:
            raise RetailerProtocolError("Sixty60 cart line is missing a product ID")
        product = products.get(product_id) if products else None
        name = _cart_line_name(raw) or (product.name if product else None)
        quantity = raw.get("quantity", 0)
        if not isinstance(quantity, int) or quantity < 0:
            raise RetailerProtocolError("Sixty60 cart line has an invalid quantity")
        lines.append(
            CartLine(
                retailer_product_id=product_id,
                retailer_line_id=raw.get("id") if isinstance(raw.get("id"), str) else None,
                name=name or f"Product name unavailable ({product_id})",
                quantity=quantity,
                unit_price=_money(raw.get("price", 0), raw.get("priceFactor", 100)),
            )
        )
    raw_total = payload.get("total", payload.get("totalPrice"))
    total = (
        _money(raw_total, payload.get("priceFactor", 100))
        if raw_total is not None
        else sum((line.line_total for line in lines), Decimal("0"))
    )
    return RetailerCart(
        retailer=Retailer.SIXTY60,
        cart_id=cart_id,
        items=lines,
        total=total,
        store_id=next(
            (
                str(line.get("storeId"))
                for line in raw_lines
                if isinstance(line, dict) and line.get("storeId")
            ),
            None,
        ),
    )


def cart_product_ids_missing_names(payload: dict[str, Any]) -> list[str]:
    raw_lines = payload.get("lineItems", [])
    if not isinstance(raw_lines, list):
        return []
    return list(
        dict.fromkeys(
            product_id
            for raw in raw_lines
            if isinstance(raw, dict)
            and str(raw.get("status", "")).lower() != "removed"
            and not _cart_line_name(raw)
            and isinstance(
                product_id := raw.get("productId") or _cart_line_product(raw).get("id"), str
            )
            and product_id
        )
    )


def orders_from_payload(payload: dict[str, Any]) -> list[RetailerOrder]:
    raw_orders = payload.get("inactiveOrderGroupSummaries")
    if not isinstance(raw_orders, list):
        raise RetailerProtocolError("Sixty60 order history had an unexpected shape")
    return [order_from_payload(raw) for raw in raw_orders if isinstance(raw, dict)]


def order_from_payload(raw: dict[str, Any]) -> RetailerOrder:
    order_id = raw.get("id") or raw.get("orderId")
    if not isinstance(order_id, (str, int)) or not str(order_id).strip():
        raise RetailerProtocolError("Sixty60 order history entry is missing an order ID")
    status = raw.get("reducedStatus") or raw.get("customerStatus") or raw.get("status")
    if isinstance(status, dict):
        status = status.get("orderStatus") or status.get("status")
    if not isinstance(status, str) or not status.strip():
        status = "unknown"

    totals = raw.get("totals") if isinstance(raw.get("totals"), dict) else {}
    raw_total = totals.get("totalPayable", raw.get("total"))
    total = None
    if raw_total is not None:
        factor = totals.get("priceFactor", raw.get("priceFactor", 100))
        total = _money(raw_total, factor)

    lines = _order_lines(raw)
    raw_count = raw.get("itemCount") or raw.get("numberOfItems")
    item_count = (
        raw_count if isinstance(raw_count, int) and not isinstance(raw_count, bool) else None
    )
    if item_count is None and lines:
        item_count = sum(line.quantity for line in lines)

    reference = raw.get("reference") or raw.get("orderNumber")
    return RetailerOrder(
        retailer=Retailer.SIXTY60,
        order_id=str(order_id),
        reference=str(reference) if isinstance(reference, (str, int)) else None,
        status=status.strip(),
        created_at=_timestamp(raw.get("createdOn") or raw.get("createdAt")),
        total=total,
        item_count=item_count,
        items=lines,
    )


def _order_lines(order: dict[str, Any]) -> list[OrderLine]:
    raw_lines = next(
        (
            value
            for key in ("lineItems", "items", "orderItems")
            if isinstance((value := order.get(key)), list)
        ),
        [],
    )
    lines: list[OrderLine] = []
    for wrapper in raw_lines:
        if not isinstance(wrapper, dict):
            continue
        raw = wrapper.get("item") if isinstance(wrapper.get("item"), dict) else wrapper
        product = _cart_line_product(raw)
        product_id = raw.get("productId") or product.get("id") or product.get("sku")
        name = _cart_line_name(raw)
        if not name:
            if not isinstance(product_id, (str, int)):
                raise RetailerProtocolError("Sixty60 order line is missing product details")
            name = f"Product name unavailable ({product_id})"
        quantity = raw.get("quantity", 1)
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity < 0:
            raise RetailerProtocolError("Sixty60 order line has an invalid quantity")
        unit_price = None
        if raw.get("price") is not None or raw.get("priceWithoutDecimal") is not None:
            price = raw.get("priceWithoutDecimal", raw.get("price"))
            unit_price = _money(price, raw.get("priceFactor", 100))
        raw_line_total = raw.get("lineTotal") or raw.get("total")
        line_total = (
            _money(raw_line_total, raw.get("priceFactor", 100))
            if raw_line_total is not None
            else unit_price * quantity
            if unit_price is not None
            else None
        )
        lines.append(
            OrderLine(
                retailer_product_id=str(product_id) if isinstance(product_id, (str, int)) else None,
                name=name,
                quantity=quantity,
                unit_price=unit_price,
                line_total=line_total,
            )
        )
    return lines


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value / 1000 if value >= 100_000_000_000 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            raise RetailerProtocolError("Sixty60 order had an invalid timestamp") from None
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise RetailerProtocolError("Sixty60 order had an invalid timestamp") from None
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed
    return None


def _optional_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _cart_line_product(line: dict[str, Any]) -> dict[str, Any]:
    for key in ("product", "productMinInfo", "productMinInfoAnnotation"):
        value = line.get(key)
        if isinstance(value, dict):
            return value
    return {}


def _cart_line_name(line: dict[str, Any]) -> str | None:
    nested = _cart_line_product(line)
    value = line.get("name") or nested.get("name") or nested.get("productName")
    return value if isinstance(value, str) and value.strip() else None


def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
    return next((payload[key] for key in keys if isinstance(payload.get(key), str)), None)
