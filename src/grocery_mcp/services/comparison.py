from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from grocery_mcp.domain.change_sets import RequestedItem
from grocery_mcp.domain.comparison import BasketEstimate
from grocery_mcp.domain.models import Retailer, RetailerProduct
from grocery_mcp.services.preferences import PreferenceService
from grocery_mcp.services.registry import AdapterRegistry


@dataclass(slots=True)
class ComparisonResult:
    query: str
    products: dict[Retailer, list[RetailerProduct]]
    warnings: list[str]


class ComparisonService:
    def __init__(
        self, adapters: AdapterRegistry, preferences: PreferenceService | None = None
    ) -> None:
        self._adapters = adapters
        self._preferences = preferences

    async def compare(
        self, query: str, retailers: list[Retailer], limit_per_retailer: int = 5
    ) -> ComparisonResult:
        if len(set(retailers)) < 2:
            raise ValueError("comparison requires at least two retailers")
        products: dict[Retailer, list[RetailerProduct]] = {}
        warnings: list[str] = []
        for retailer in retailers:
            found = await self._adapters.get(retailer).search_products(query, limit_per_retailer)
            units = {product.size.unit for product in found if product.size}
            comparable = len(units) == 1 and all(product.size is not None for product in found)
            if comparable:
                products[retailer] = sorted(
                    found, key=lambda product: product.unit_price or Decimal("Infinity")
                )
            else:
                products[retailer] = sorted(found, key=lambda product: product.price)
            if any(product.size is None for product in found):
                warnings.append(
                    f"Some {retailer.value} pack sizes could not be normalized; "
                    "prices may not be comparable"
                )
            if len(units) > 1:
                warnings.append(
                    f"{retailer.value} results use different units and must not be "
                    "ranked as equivalent"
                )
        return ComparisonResult(query=query, products=products, warnings=warnings)

    async def compare_baskets(
        self, items: list[RequestedItem], retailers: list[Retailer]
    ) -> list[BasketEstimate]:
        if not items:
            raise ValueError("at least one item is required")
        if len(set(retailers)) < 2:
            raise ValueError("basket comparison requires at least two retailers")
        estimates: list[BasketEstimate] = []
        for retailer in retailers:
            total = Decimal("0")
            missing: list[str] = []
            warnings: list[str] = []
            adapter = self._adapters.get(retailer)
            for requested in items:
                label = requested.household_item or requested.query or "unknown"
                try:
                    if requested.household_item and self._preferences:
                        resolved = await self._preferences.resolve(
                            requested.household_item, retailer
                        )
                        if resolved.mapping:
                            product = await adapter.get_product(
                                resolved.mapping.retailer_product_id
                            )
                        else:
                            candidates = await adapter.search_products(
                                resolved.item.display_name, 5
                            )
                            ranked = self._preferences.rank(candidates, resolved)
                            product = ranked[0]
                            warnings.extend(resolved.warnings)
                    else:
                        candidates = await adapter.search_products(label, 5)
                        product = candidates[0]
                    if product.in_stock is False:
                        missing.append(label)
                    else:
                        total += product.price * requested.quantity
                except (IndexError, KeyError, ValueError):
                    missing.append(label)
            estimates.append(
                BasketEstimate(
                    retailer=retailer,
                    total=total.quantize(Decimal("0.01")),
                    missing_items=missing,
                    warnings=list(dict.fromkeys(warnings)),
                )
            )
        return estimates
