from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from grocery_mcp.domain.models import CartLine, Retailer, RetailerCart, RetailerProduct
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
