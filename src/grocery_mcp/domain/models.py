from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


class Retailer(StrEnum):
    SIXTY60 = "sixty60"
    WOOLWORTHS = "woolworths"


class Currency(StrEnum):
    ZAR = "ZAR"


class ConstraintLevel(StrEnum):
    MUST = "must"
    PREFER = "prefer"
    AVOID = "avoid"
    NEVER = "never"


class ProductSize(BaseModel):
    value: Decimal
    unit: str

    @field_validator("value")
    @classmethod
    def positive_value(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("size value must be positive")
        return value


class RetailerProduct(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retailer: Retailer
    retailer_product_id: str
    sku: str | None = None
    name: str
    brand: str | None = None
    size: ProductSize | None = None
    price: Decimal
    currency: Currency = Currency.ZAR
    in_stock: bool | None = None
    image_url: str | None = None
    product_url: str | None = None
    store_id: str | None = Field(default=None, exclude=True)
    raw: dict[str, Any] = Field(default_factory=dict, exclude=True)

    @field_validator("price")
    @classmethod
    def non_negative_price(cls, value: Decimal) -> Decimal:
        if value < 0:
            raise ValueError("price must not be negative")
        return value

    @computed_field
    @property
    def unit_price(self) -> Decimal | None:
        if self.size is None or self.size.value == 0:
            return None
        return (self.price / self.size.value).quantize(Decimal("0.01"))

    @computed_field
    @property
    def unit_price_unit(self) -> str | None:
        return f"ZAR/{self.size.unit}" if self.size else None


class CartLine(BaseModel):
    retailer_product_id: str
    name: str
    quantity: int
    unit_price: Decimal
    currency: Currency = Currency.ZAR
    retailer_line_id: str | None = Field(default=None, exclude=True)
    product: RetailerProduct | None = None

    @field_validator("quantity")
    @classmethod
    def non_negative_quantity(cls, value: int) -> int:
        if value < 0:
            raise ValueError("quantity must not be negative")
        return value

    @computed_field
    @property
    def line_total(self) -> Decimal:
        return (self.unit_price * self.quantity).quantize(Decimal("0.01"))


class RetailerCart(BaseModel):
    retailer: Retailer
    cart_id: str
    items: list[CartLine] = Field(default_factory=list)
    total: Decimal
    currency: Currency = Currency.ZAR
    store_id: str | None = Field(default=None, exclude=True)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def snapshot_hash(self) -> str:
        stable = {
            "retailer": self.retailer.value,
            "cart_id": self.cart_id,
            "store_id": self.store_id,
            "items": sorted(
                [
                    {
                        "id": line.retailer_product_id,
                        "line_id": line.retailer_line_id,
                        "quantity": line.quantity,
                        "unit_price": str(line.unit_price),
                    }
                    for line in self.items
                ],
                key=lambda item: (item["id"], item["line_id"] or ""),
            ),
        }
        encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class RetailerHealth(BaseModel):
    retailer: Retailer
    catalogue: bool
    authentication: bool
    cart_read: bool
    cart_write: bool
    detail: str | None = None
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
