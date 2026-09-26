from __future__ import annotations

import re
from decimal import Decimal

from grocery_mcp.domain.models import ProductSize

_MULTIPACK = re.compile(
    r"(?<!\w)(\d+)\s*[x\u00d7]\s*(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml)(?!\w)",
    re.IGNORECASE,
)
_SINGLE = re.compile(r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(kg|g|l|ml)(?!\w)", re.IGNORECASE)
_COUNT = re.compile(r"(?<!\w)(\d+)\s*(?:pack|pk|pieces?|units?)(?!\w)", re.IGNORECASE)


def extract_product_size(name: str) -> ProductSize | None:
    multipack = _MULTIPACK.search(name)
    if multipack:
        count = Decimal(multipack.group(1))
        value = count * Decimal(multipack.group(2).replace(",", "."))
        return _canonical_size(value, multipack.group(3))
    single = _SINGLE.search(name)
    if single:
        return _canonical_size(Decimal(single.group(1).replace(",", ".")), single.group(2))
    count = _COUNT.search(name)
    if count:
        return ProductSize(value=Decimal(count.group(1)), unit="item")
    return None


def _canonical_size(value: Decimal, unit: str) -> ProductSize:
    normalized = unit.lower()
    if normalized == "ml":
        return ProductSize(value=value / 1000, unit="L")
    if normalized == "g":
        return ProductSize(value=value / 1000, unit="kg")
    return ProductSize(value=value, unit={"l": "L", "kg": "kg"}[normalized])
