from __future__ import annotations

from decimal import Decimal

import pytest

from grocery_mcp.domain.change_sets import RequestedItem
from grocery_mcp.domain.models import ConstraintLevel, ProductSize, Retailer, RetailerProduct
from grocery_mcp.domain.preferences import CanonicalItem, ItemPreference, ResolvedHouseholdItem
from grocery_mcp.retailers.base import RetailerUnavailableError
from grocery_mcp.services.comparison import ComparisonService
from grocery_mcp.services.preferences import PreferenceService, PreferenceViolationError
from grocery_mcp.services.registry import AdapterRegistry
from grocery_mcp.services.search import SearchService


def product(
    retailer: Retailer,
    product_id: str,
    name: str,
    price: str,
    size: ProductSize | None = None,
    *,
    in_stock: bool | None = True,
) -> RetailerProduct:
    return RetailerProduct(
        retailer=retailer,
        retailer_product_id=product_id,
        name=name,
        price=Decimal(price),
        size=size,
        in_stock=in_stock,
    )


class StubAdapter:
    def __init__(self, retailer: Retailer, products: list[RetailerProduct], error=None) -> None:
        self.retailer = retailer
        self.products = products
        self.error = error
        self.searches: list[str] = []

    async def search_products(self, query: str, limit: int = 20) -> list[RetailerProduct]:
        if self.error:
            raise self.error
        self.searches.append(query)
        return self.products[:limit]

    async def get_product(self, retailer_product_id: str) -> RetailerProduct:
        if self.error:
            raise self.error
        for candidate in self.products:
            if candidate.retailer_product_id == retailer_product_id:
                return candidate
        raise KeyError(retailer_product_id)

    async def close(self) -> None:
        return None


def registry(**adapters: StubAdapter) -> AdapterRegistry:
    return AdapterRegistry(
        {Retailer(name): adapter for name, adapter in adapters.items()}  # type: ignore[dict-item]
    )


def resolved(*preferences: ItemPreference) -> ResolvedHouseholdItem:
    return ResolvedHouseholdItem(
        item=CanonicalItem(key="milk", display_name="Milk", preferences=list(preferences)),
        mapping=None,
    )


# Search


async def test_search_returns_every_retailer_that_answered() -> None:
    adapters = registry(
        sixty60=StubAdapter(Retailer.SIXTY60, [product(Retailer.SIXTY60, "a", "Milk 2L", "40")]),
        woolworths=StubAdapter(
            Retailer.WOOLWORTHS, [product(Retailer.WOOLWORTHS, "b", "Milk 2L", "45")]
        ),
    )

    results = await SearchService(adapters).search("milk")

    assert set(results) == {Retailer.SIXTY60, Retailer.WOOLWORTHS}


async def test_search_drops_a_failing_retailer_rather_than_the_whole_result() -> None:
    adapters = registry(
        sixty60=StubAdapter(Retailer.SIXTY60, [], error=RetailerUnavailableError("down")),
        woolworths=StubAdapter(
            Retailer.WOOLWORTHS, [product(Retailer.WOOLWORTHS, "b", "Milk 2L", "45")]
        ),
    )

    results = await SearchService(adapters).search("milk")

    assert set(results) == {Retailer.WOOLWORTHS}


async def test_search_raises_when_no_retailer_answered() -> None:
    adapters = registry(
        sixty60=StubAdapter(Retailer.SIXTY60, [], error=RetailerUnavailableError("down")),
        woolworths=StubAdapter(Retailer.WOOLWORTHS, [], error=RetailerUnavailableError("down")),
    )

    with pytest.raises(RuntimeError, match="all retailer searches failed"):
        await SearchService(adapters).search("milk")


@pytest.mark.parametrize(("query", "limit"), [("   ", 10), ("milk", 0), ("milk", 51)])
async def test_search_rejects_unusable_arguments(query: str, limit: int) -> None:
    adapters = registry(sixty60=StubAdapter(Retailer.SIXTY60, []))

    with pytest.raises(ValueError):
        await SearchService(adapters).search(query, limit=limit)


# Comparison


async def test_comparison_ranks_by_unit_price_when_units_agree() -> None:
    litre = ProductSize(value=Decimal("1"), unit="L")
    two_litre = ProductSize(value=Decimal("2"), unit="L")
    adapters = registry(
        sixty60=StubAdapter(
            Retailer.SIXTY60,
            [
                product(Retailer.SIXTY60, "a", "Milk 1L", "25", litre),
                product(Retailer.SIXTY60, "b", "Milk 2L", "40", two_litre),
            ],
        ),
        woolworths=StubAdapter(
            Retailer.WOOLWORTHS, [product(Retailer.WOOLWORTHS, "c", "Milk 1L", "30", litre)]
        ),
    )

    result = await ComparisonService(adapters).compare(
        "milk", [Retailer.SIXTY60, Retailer.WOOLWORTHS]
    )

    assert [item.retailer_product_id for item in result.products[Retailer.SIXTY60]] == ["b", "a"]
    assert result.warnings == []


async def test_comparison_warns_instead_of_ranking_incomparable_packs() -> None:
    adapters = registry(
        sixty60=StubAdapter(
            Retailer.SIXTY60,
            [
                product(Retailer.SIXTY60, "a", "Milk 1L", "25", ProductSize(value=1, unit="L")),
                product(
                    Retailer.SIXTY60, "b", "Cheese 500g", "40", ProductSize(value=1, unit="kg")
                ),
                product(Retailer.SIXTY60, "c", "Milk carton", "20"),
            ],
        ),
        woolworths=StubAdapter(Retailer.WOOLWORTHS, []),
    )

    result = await ComparisonService(adapters).compare(
        "milk", [Retailer.SIXTY60, Retailer.WOOLWORTHS]
    )

    assert [item.retailer_product_id for item in result.products[Retailer.SIXTY60]] == [
        "c",
        "a",
        "b",
    ]
    assert any("could not be normalized" in warning for warning in result.warnings)
    assert any("different units" in warning for warning in result.warnings)


async def test_comparison_requires_two_distinct_retailers() -> None:
    adapters = registry(sixty60=StubAdapter(Retailer.SIXTY60, []))

    with pytest.raises(ValueError, match="at least two retailers"):
        await ComparisonService(adapters).compare("milk", [Retailer.SIXTY60, Retailer.SIXTY60])


async def test_basket_totals_use_quantity_and_report_missing_items() -> None:
    adapters = registry(
        sixty60=StubAdapter(Retailer.SIXTY60, [product(Retailer.SIXTY60, "a", "Milk 2L", "40")]),
        woolworths=StubAdapter(
            Retailer.WOOLWORTHS,
            [product(Retailer.WOOLWORTHS, "b", "Milk 2L", "45", in_stock=False)],
        ),
    )

    estimates = await ComparisonService(adapters).compare_baskets(
        [RequestedItem(query="milk", quantity=3)], [Retailer.SIXTY60, Retailer.WOOLWORTHS]
    )

    by_retailer = {estimate.retailer: estimate for estimate in estimates}
    assert by_retailer[Retailer.SIXTY60].total == Decimal("120.00")
    assert by_retailer[Retailer.SIXTY60].missing_items == []
    assert by_retailer[Retailer.WOOLWORTHS].total == Decimal("0.00")
    assert by_retailer[Retailer.WOOLWORTHS].missing_items == ["milk"]


async def test_basket_records_an_unsearchable_item_as_missing() -> None:
    adapters = registry(
        sixty60=StubAdapter(Retailer.SIXTY60, []),
        woolworths=StubAdapter(
            Retailer.WOOLWORTHS, [product(Retailer.WOOLWORTHS, "b", "Milk 2L", "45")]
        ),
    )

    estimates = await ComparisonService(adapters).compare_baskets(
        [RequestedItem(query="milk")], [Retailer.SIXTY60, Retailer.WOOLWORTHS]
    )

    assert estimates[0].missing_items == ["milk"]
    assert estimates[0].total == Decimal("0.00")


async def test_basket_requires_items_and_two_retailers() -> None:
    adapters = registry(sixty60=StubAdapter(Retailer.SIXTY60, []))
    service = ComparisonService(adapters)

    with pytest.raises(ValueError, match="at least one item"):
        await service.compare_baskets([], [Retailer.SIXTY60, Retailer.WOOLWORTHS])
    with pytest.raises(ValueError, match="at least two retailers"):
        await service.compare_baskets([RequestedItem(query="milk")], [Retailer.SIXTY60])


# Preference ranking


def test_ranking_rejects_every_candidate_that_breaks_a_hard_constraint() -> None:
    candidates = [
        product(Retailer.SIXTY60, "a", "Low Fat Milk 2L", "30"),
        product(Retailer.SIXTY60, "b", "Full Cream Milk 2L", "40"),
        product(Retailer.SIXTY60, "c", "Full Cream Milk 1L", "25"),
    ]
    preferences = resolved(
        ItemPreference(attribute="name", value="full cream", level=ConstraintLevel.MUST),
        ItemPreference(attribute="name", value="low fat", level=ConstraintLevel.NEVER),
    )

    ranked = PreferenceService.rank(candidates, preferences)

    assert [item.retailer_product_id for item in ranked] == ["c", "b"]


def test_ranking_raises_when_nothing_satisfies_a_hard_constraint() -> None:
    candidates = [product(Retailer.SIXTY60, "a", "Low Fat Milk 2L", "30")]
    preferences = resolved(
        ItemPreference(attribute="name", value="full cream", level=ConstraintLevel.MUST)
    )

    with pytest.raises(PreferenceViolationError):
        PreferenceService.rank(candidates, preferences)


def test_ranking_prefers_a_soft_match_over_a_cheaper_avoided_product() -> None:
    candidates = [
        product(Retailer.SIXTY60, "cheap", "Milk 2L", "30"),
        product(Retailer.SIXTY60, "organic", "Organic Milk 2L", "50"),
    ]
    preferences = resolved(
        ItemPreference(attribute="name", value="organic", level=ConstraintLevel.PREFER)
    )

    ranked = PreferenceService.rank(candidates, preferences)

    assert ranked[0].retailer_product_id == "organic"


def test_registry_rejects_an_unconfigured_retailer() -> None:
    adapters = registry(sixty60=StubAdapter(Retailer.SIXTY60, []))

    with pytest.raises(ValueError, match="not configured"):
        adapters.get(Retailer.WOOLWORTHS)
