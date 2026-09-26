from decimal import Decimal

import pytest

from grocery_mcp.domain.change_sets import RequestedItem
from grocery_mcp.domain.models import ProductSize, Retailer, RetailerProduct
from grocery_mcp.domain.products import extract_product_size


def test_unit_price_uses_normalized_size() -> None:
    product = RetailerProduct(
        retailer=Retailer.WOOLWORTHS,
        retailer_product_id="milk",
        name="Milk 2L",
        price=Decimal("40"),
        size=ProductSize(value=Decimal("2"), unit="L"),
    )
    assert product.unit_price == Decimal("20.00")


@pytest.mark.parametrize(
    "payload",
    [
        {"query": "milk", "household_item": "usual milk"},
        {},
        {"query": "milk", "quantity": 0},
    ],
)
def test_requested_item_rejects_ambiguous_or_unsafe_input(payload) -> None:
    with pytest.raises(ValueError):
        RequestedItem.model_validate(payload)


@pytest.mark.parametrize(
    ("name", "value", "unit"),
    [
        ("Fresh Milk 2 L", "2", "L"),
        ("Long Life Milk 6 x 1 L", "6", "L"),
        ("Coffee 500 g", "0.5", "kg"),
        ("Dishwasher Tablets 30 pack", "30", "item"),
    ],
)
def test_product_size_parser_normalizes_units(name: str, value: str, unit: str) -> None:
    size = extract_product_size(name)
    assert size is not None
    assert size.value == Decimal(value)
    assert size.unit == unit
