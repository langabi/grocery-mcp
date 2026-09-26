from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from grocery_mcp.domain.change_sets import ChangeAction, ChangeSetStatus, RequestedItem
from grocery_mcp.domain.models import (
    CartLine,
    Retailer,
    RetailerCart,
    RetailerHealth,
    RetailerProduct,
)
from grocery_mcp.domain.preferences import CanonicalItem
from grocery_mcp.services.cart_service import CartService, ChangeSetStateError
from grocery_mcp.services.preferences import PreferenceService
from grocery_mcp.services.registry import AdapterRegistry
from grocery_mcp.storage.db import ChangeSetRow, Database
from grocery_mcp.storage.repositories import (
    AuditRepository,
    ChangeSetRepository,
    PreferenceRepository,
)


class FakeAdapter:
    def __init__(self) -> None:
        self.product = RetailerProduct(
            retailer=Retailer.SIXTY60,
            retailer_product_id="milk-1",
            name="Full Cream Milk 2L",
            price=Decimal("40.00"),
            in_stock=True,
        )
        self.cart = RetailerCart(
            retailer=Retailer.SIXTY60,
            cart_id="cart-1",
            items=[],
            total=Decimal("100.00"),
        )
        self.write_calls = 0

    async def search_products(self, query: str, limit: int = 20):
        return [self.product]

    async def get_product(self, retailer_product_id: str):
        return self.product

    async def get_cart(self):
        return self.cart.model_copy(deep=True)

    async def add_to_cart(self, retailer_product_id: str, quantity: int):
        self.write_calls += 1
        self.cart.items.append(
            CartLine(
                retailer_product_id=retailer_product_id,
                name=self.product.name,
                quantity=quantity,
                unit_price=self.product.price,
            )
        )
        self.cart.total += self.product.price * quantity
        return await self.get_cart()

    async def remove_from_cart(self, retailer_product_id: str):
        self.write_calls += 1
        self.cart.items = [
            line for line in self.cart.items if line.retailer_product_id != retailer_product_id
        ]
        return await self.get_cart()

    async def set_quantity(self, retailer_product_id: str, quantity: int):
        self.write_calls += 1
        if quantity == 0:
            return await self.remove_from_cart(retailer_product_id)
        for line in self.cart.items:
            if line.retailer_product_id == retailer_product_id:
                line.quantity = quantity
        return await self.get_cart()

    async def health_check(self):
        return RetailerHealth(
            retailer=Retailer.SIXTY60,
            catalogue=True,
            authentication=True,
            cart_read=True,
            cart_write=True,
        )

    async def close(self):
        return None


@pytest.fixture
async def service(tmp_path):
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    await database.initialise()
    adapter = FakeAdapter()
    registry = AdapterRegistry({Retailer.SIXTY60: adapter})  # type: ignore[dict-item]
    change_sets = ChangeSetRepository(database)
    result = (
        CartService(
            registry,
            PreferenceService(PreferenceRepository(database)),
            change_sets,
            AuditRepository(database),
        ),
        adapter,
        change_sets,
        database,
    )
    yield result
    await database.close()


async def test_prepare_never_mutates_cart(service) -> None:
    carts, adapter, _repository, _database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=2)]
    )

    assert prepared.status == ChangeSetStatus.PREPARED
    assert prepared.estimated_cart_delta == Decimal("80.00")
    assert prepared.expected_cart_total == Decimal("180.00")
    assert adapter.write_calls == 0
    assert adapter.cart.items == []


async def test_apply_is_idempotent(service) -> None:
    carts, adapter, _repository, _database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=2)]
    )

    first = await carts.apply_changes(prepared.change_set_id)
    second = await carts.apply_changes(prepared.change_set_id)

    assert first.status == ChangeSetStatus.APPLIED
    assert second == first
    assert adapter.write_calls == 1
    assert adapter.cart.items[0].quantity == 2


async def test_cart_drift_requires_reconfirmation_without_write(service) -> None:
    carts, adapter, _repository, _database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=1)]
    )
    adapter.cart.total = Decimal("105.00")
    adapter.cart.items.append(
        CartLine(
            retailer_product_id="bread-1",
            name="Bread",
            quantity=1,
            unit_price=Decimal("5.00"),
        )
    )

    result = await carts.apply_changes(prepared.change_set_id)

    assert result.status == ChangeSetStatus.REQUIRES_RECONFIRMATION
    assert result.reason == "cart_changed_since_proposal"
    assert adapter.write_calls == 0


async def test_expired_change_set_never_writes(service) -> None:
    carts, adapter, _repository, database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=1)]
    )
    async with database.session() as session, session.begin():
        row = await session.get(ChangeSetRow, prepared.change_set_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)

    result = await carts.apply_changes(prepared.change_set_id)

    assert result.status == ChangeSetStatus.EXPIRED
    assert adapter.write_calls == 0


async def test_failed_apply_cannot_be_retried(service) -> None:
    carts, adapter, _repository, _database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=1)]
    )
    adapter.product.in_stock = False

    with pytest.raises(ChangeSetStateError):
        await carts.apply_changes(prepared.change_set_id)
    with pytest.raises(ChangeSetStateError, match="failed"):
        await carts.apply_changes(prepared.change_set_id)
    assert adapter.write_calls == 0


async def test_household_default_quantity_is_used_when_quantity_is_omitted(service) -> None:
    carts, adapter, _repository, database = service
    await PreferenceRepository(database).upsert_item(
        CanonicalItem(
            key="milk",
            display_name="Full Cream Milk 2L",
            default_quantity=2,
            aliases=["usual milk"],
        )
    )

    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(household_item="usual milk")]
    )

    assert prepared.changes[0].target_quantity == 2
    assert prepared.estimated_cart_delta == Decimal("80.00")
    assert adapter.write_calls == 0


async def test_prepare_rejects_an_unavailable_price(service) -> None:
    carts, adapter, _repository, _database = service
    adapter.product.price = Decimal("0")

    with pytest.raises(ValueError, match="price is unavailable"):
        await carts.prepare_changes(Retailer.SIXTY60, [RequestedItem(query="milk")])
    assert adapter.write_calls == 0


async def test_concurrent_change_sets_are_serialized_and_only_one_writes(service) -> None:
    carts, adapter, _repository, _database = service
    first = await carts.prepare_changes(Retailer.SIXTY60, [RequestedItem(query="milk")])
    second = await carts.prepare_changes(Retailer.SIXTY60, [RequestedItem(query="milk")])
    original_get_cart = adapter.get_cart
    active_reads = 0
    max_active_reads = 0

    async def slow_get_cart():
        nonlocal active_reads, max_active_reads
        active_reads += 1
        max_active_reads = max(max_active_reads, active_reads)
        await asyncio.sleep(0.01)
        try:
            return await original_get_cart()
        finally:
            active_reads -= 1

    adapter.get_cart = slow_get_cart  # type: ignore[method-assign]
    results = await asyncio.gather(
        carts.apply_changes(first.change_set_id),
        carts.apply_changes(second.change_set_id),
    )

    assert {result.status for result in results} == {
        ChangeSetStatus.APPLIED,
        ChangeSetStatus.REQUIRES_RECONFIRMATION,
    }
    assert adapter.write_calls == 1
    assert max_active_reads == 1


def _stock_line(adapter, quantity: int) -> None:
    adapter.cart.items = [
        CartLine(
            retailer_product_id=adapter.product.retailer_product_id,
            name=adapter.product.name,
            quantity=quantity,
            unit_price=adapter.product.price,
        )
    ]


async def test_set_mode_reduces_an_existing_quantity(service) -> None:
    carts, adapter, _repository, _database = service
    _stock_line(adapter, 3)

    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=1, mode="set")]
    )

    assert [change.action for change in prepared.changes] == [ChangeAction.REDUCE]
    assert prepared.changes[0].previous_quantity == 3
    assert prepared.changes[0].target_quantity == 1
    assert prepared.estimated_cart_delta == Decimal("-80.00")
    assert adapter.write_calls == 0

    result = await carts.apply_changes(prepared.change_set_id)

    assert result.status == ChangeSetStatus.APPLIED
    assert [line.quantity for line in adapter.cart.items] == [1]


async def test_set_mode_zero_removes_the_product(service) -> None:
    carts, adapter, _repository, _database = service
    _stock_line(adapter, 2)

    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=0, mode="set")]
    )

    assert [change.action for change in prepared.changes] == [ChangeAction.REMOVE]
    assert prepared.estimated_cart_delta == Decimal("-80.00")

    result = await carts.apply_changes(prepared.change_set_id)

    assert result.status == ChangeSetStatus.APPLIED
    assert adapter.cart.items == []


async def test_removal_is_not_blocked_by_stock_or_price_movement(service) -> None:
    carts, adapter, _repository, _database = service
    _stock_line(adapter, 1)
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=0, mode="set")]
    )
    adapter.product = adapter.product.model_copy(
        update={"in_stock": False, "price": Decimal("99.00")}
    )

    result = await carts.apply_changes(prepared.change_set_id)

    assert result.status == ChangeSetStatus.APPLIED
    assert adapter.cart.items == []


async def test_increase_is_still_blocked_by_stock(service) -> None:
    carts, adapter, _repository, _database = service
    prepared = await carts.prepare_changes(
        Retailer.SIXTY60, [RequestedItem(query="milk", quantity=1)]
    )
    adapter.product = adapter.product.model_copy(update={"in_stock": False})

    with pytest.raises(ChangeSetStateError):
        await carts.apply_changes(prepared.change_set_id)
    assert adapter.write_calls == 0


async def test_a_proposal_that_changes_nothing_is_refused(service) -> None:
    carts, adapter, _repository, _database = service
    _stock_line(adapter, 2)

    with pytest.raises(ValueError, match="already matches"):
        await carts.prepare_changes(
            Retailer.SIXTY60, [RequestedItem(query="milk", quantity=2, mode="set")]
        )
    assert adapter.write_calls == 0


async def test_add_mode_requires_a_positive_quantity() -> None:
    with pytest.raises(ValueError, match="positive"):
        RequestedItem(query="milk", quantity=0)
