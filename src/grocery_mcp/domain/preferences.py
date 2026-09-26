from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field, field_validator

from grocery_mcp.domain.models import ConstraintLevel, Retailer


class ItemPreference(BaseModel):
    attribute: str
    value: str
    level: ConstraintLevel


class CanonicalItem(BaseModel):
    key: str
    display_name: str
    default_quantity: int = 1
    aliases: list[str] = Field(default_factory=list)
    preferences: list[ItemPreference] = Field(default_factory=list)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        normalized = value.strip().lower().replace(" ", "_")
        if not normalized:
            raise ValueError("canonical item key must not be empty")
        return normalized


class RetailerItemMapping(BaseModel):
    canonical_key: str
    retailer: Retailer
    retailer_product_id: str
    product_name: str


@dataclass(slots=True)
class ResolvedHouseholdItem:
    item: CanonicalItem
    mapping: RetailerItemMapping | None
    warnings: list[str] = field(default_factory=list)
