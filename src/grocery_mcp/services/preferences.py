from __future__ import annotations

from grocery_mcp.domain.models import ConstraintLevel, Retailer, RetailerProduct
from grocery_mcp.domain.preferences import ResolvedHouseholdItem
from grocery_mcp.storage.repositories import PreferenceRepository


class PreferenceViolationError(ValueError):
    pass


class PreferenceService:
    def __init__(self, repository: PreferenceRepository) -> None:
        self._repository = repository

    async def resolve(self, item: str, retailer: Retailer) -> ResolvedHouseholdItem:
        result = await self._repository.resolve(item, retailer)
        if result is None:
            raise KeyError(f"unknown household item: {item}")
        canonical, mapping = result
        warnings = [] if mapping else [f"No preferred {retailer.value} product is mapped"]
        return ResolvedHouseholdItem(item=canonical, mapping=mapping, warnings=warnings)

    @staticmethod
    def rank(
        products: list[RetailerProduct], resolved: ResolvedHouseholdItem
    ) -> list[RetailerProduct]:
        def score(product: RetailerProduct) -> tuple[int, int, object]:
            haystack = f"{product.brand or ''} {product.name}".lower()
            hard_rejected = False
            preference_score = 0
            for preference in resolved.item.preferences:
                matched = preference.value.lower() in haystack
                if preference.level == ConstraintLevel.NEVER and matched:
                    hard_rejected = True
                elif preference.level == ConstraintLevel.MUST and not matched:
                    hard_rejected = True
                elif preference.level == ConstraintLevel.PREFER and matched:
                    preference_score += 5
                elif preference.level == ConstraintLevel.AVOID and matched:
                    preference_score -= 3
            mapped = int(
                resolved.mapping is not None
                and product.retailer_product_id == resolved.mapping.retailer_product_id
            )
            return (int(not hard_rejected), mapped * 100 + preference_score, -product.price)

        ranked = sorted(products, key=score, reverse=True)
        allowed = [product for product in ranked if score(product)[0] == 1]
        if not allowed and products:
            raise PreferenceViolationError("all candidates violate hard household constraints")
        return allowed
