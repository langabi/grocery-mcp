from __future__ import annotations

from pydantic import BaseModel, Field

from grocery_mcp.domain.change_sets import RequestedItem
from grocery_mcp.domain.models import Retailer, RetailerHealth, RetailerProduct
from grocery_mcp.domain.preferences import CanonicalItem, RetailerItemMapping


class SearchResponse(BaseModel):
    results: dict[Retailer, list[RetailerProduct]]


class ComparisonResponse(BaseModel):
    query: str
    results: dict[Retailer, list[RetailerProduct]]
    warnings: list[str] = Field(default_factory=list)


class ResolveHouseholdItemResponse(BaseModel):
    item: CanonicalItem
    retailer_mapping: RetailerItemMapping | None
    warnings: list[str] = Field(default_factory=list)


class PrepareCartChangesInput(BaseModel):
    retailer: Retailer
    items: list[RequestedItem]


class ServiceHealth(BaseModel):
    status: str
    retailers: list[RetailerHealth]
