from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from grocery_mcp.domain.models import Retailer, RetailerProduct


class ChangeSetStatus(StrEnum):
    PREPARED = "prepared"
    APPLYING = "applying"
    APPLIED = "applied"
    DISCARDED = "discarded"
    EXPIRED = "expired"
    FAILED = "failed"
    REQUIRES_RECONFIRMATION = "requires_reconfirmation"


class RequestedItemMode(StrEnum):
    ADD = "add"
    SET = "set"


class RequestedItem(BaseModel):
    household_item: str | None = None
    query: str | None = None
    quantity: int = 1
    mode: RequestedItemMode = Field(
        default=RequestedItemMode.ADD,
        description=(
            "add: quantity is added to whatever the cart already holds. "
            "set: quantity becomes the exact cart quantity, and 0 removes the product. "
            "Only prepare_cart_changes reads this field."
        ),
    )

    @field_validator("quantity")
    @classmethod
    def non_negative_quantity(cls, value: int) -> int:
        if value < 0:
            raise ValueError("quantity must not be negative")
        return value

    def model_post_init(self, __context: object) -> None:
        if bool(self.household_item) == bool(self.query):
            raise ValueError("provide exactly one of household_item or query")
        if self.mode is RequestedItemMode.ADD and self.quantity <= 0:
            raise ValueError("quantity must be positive unless mode is 'set'")


class ChangeAction(StrEnum):
    INCREASE = "increase"
    REDUCE = "reduce"
    REMOVE = "remove"


class PreparedChange(BaseModel):
    action: ChangeAction
    product: RetailerProduct
    previous_quantity: int
    target_quantity: int

    @field_validator("target_quantity")
    @classmethod
    def non_negative_target(cls, value: int) -> int:
        if value < 0:
            raise ValueError("target quantity must not be negative")
        return value

    @property
    def quantity_delta(self) -> int:
        return self.target_quantity - self.previous_quantity


class PreparedChangeSet(BaseModel):
    change_set_id: str
    status: ChangeSetStatus
    retailer: Retailer
    created_at: datetime
    expires_at: datetime
    changes: list[PreparedChange]
    estimated_cart_delta: Decimal
    expected_cart_total: Decimal
    warnings: list[str] = Field(default_factory=list)


class ApplyResult(BaseModel):
    change_set_id: str
    status: ChangeSetStatus
    cart_total: Decimal | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
