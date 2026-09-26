from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from grocery_mcp.domain.models import Retailer


class BasketEstimate(BaseModel):
    retailer: Retailer
    total: Decimal
    missing_items: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
