from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from grocery_mcp.domain.change_sets import (
    ApplyResult,
    ChangeAction,
    ChangeSetStatus,
    PreparedChange,
    PreparedChangeSet,
    RequestedItem,
    RequestedItemMode,
)
from grocery_mcp.domain.models import Retailer, RetailerProduct
from grocery_mcp.services.preferences import PreferenceService
from grocery_mcp.services.registry import AdapterRegistry
from grocery_mcp.storage.repositories import AuditRepository, ChangeSetRepository


class ChangeSetNotFoundError(KeyError):
    pass


class ChangeSetStateError(RuntimeError):
    pass


class CartService:
    def __init__(
        self,
        adapters: AdapterRegistry,
        preferences: PreferenceService,
        change_sets: ChangeSetRepository,
        audit: AuditRepository,
        *,
        ttl_seconds: int = 900,
        price_tolerance_percent: float = 5.0,
    ) -> None:
        self._adapters = adapters
        self._preferences = preferences
        self._change_sets = change_sets
        self._audit = audit
        self._ttl_seconds = ttl_seconds
        self._price_tolerance_percent = Decimal(str(price_tolerance_percent))
        self._apply_locks = {retailer: asyncio.Lock() for retailer in Retailer}

    async def get_cart(self, retailer: Retailer):
        return await self._adapters.get(retailer).get_cart()

    async def _resolve_product(
        self, retailer: Retailer, request: RequestedItem
    ) -> tuple[RetailerProduct, list[str], int]:
        adapter = self._adapters.get(retailer)
        if request.household_item:
            resolved = await self._preferences.resolve(request.household_item, retailer)
            if resolved.mapping:
                product = await adapter.get_product(resolved.mapping.retailer_product_id)
                ranked = self._preferences.rank([product], resolved)
                quantity = (
                    request.quantity
                    if "quantity" in request.model_fields_set
                    else resolved.item.default_quantity
                )
                return ranked[0], resolved.warnings, quantity
            candidates = await adapter.search_products(resolved.item.display_name, 10)
            ranked = self._preferences.rank(candidates, resolved)
            if not ranked:
                raise ValueError(f"no product found for household item {request.household_item}")
            quantity = (
                request.quantity
                if "quantity" in request.model_fields_set
                else resolved.item.default_quantity
            )
            return (
                ranked[0],
                [*resolved.warnings, "Selected the highest-ranked search result"],
                quantity,
            )

        assert request.query is not None
        products = await adapter.search_products(request.query, 10)
        if not products:
            raise ValueError(f"no product found for query {request.query}")
        return (
            products[0],
            ["Selected the first retailer search result; review before applying"],
            request.quantity,
        )

    async def prepare_changes(
        self, retailer: Retailer, items: list[RequestedItem]
    ) -> PreparedChangeSet:
        if not items:
            raise ValueError("at least one item is required")
        adapter = self._adapters.get(retailer)
        cart = await adapter.get_cart()
        current_quantities: dict[str, int] = {}
        for line in cart.items:
            current_quantities[line.retailer_product_id] = (
                current_quantities.get(line.retailer_product_id, 0) + line.quantity
            )

        targets: dict[str, tuple[RetailerProduct, int]] = {}
        normalized_requests: list[RequestedItem] = []
        warnings: list[str] = []
        for request in items:
            product, item_warnings, quantity = await self._resolve_product(retailer, request)
            product_id = product.retailer_product_id
            known, running_target = targets.get(
                product_id, (product, current_quantities.get(product_id, 0))
            )
            if request.mode is RequestedItemMode.SET:
                target = quantity
            else:
                target = running_target + quantity
            targets[product_id] = (known, target)
            normalized_requests.append(request.model_copy(update={"quantity": quantity}))
            warnings.extend(item_warnings)

        changes: list[PreparedChange] = []
        delta = Decimal("0")
        for product, target in targets.values():
            previous = current_quantities.get(product.retailer_product_id, 0)
            if target == previous:
                warnings.append(f"{product.name} already has the requested quantity in the cart")
                continue
            if product.price <= 0:
                if target > previous:
                    raise ValueError(f"product price is unavailable: {product.name}")
                warnings.append(f"The estimate excludes {product.name}; its price is unavailable")
            changes.append(
                PreparedChange(
                    action=_action(previous, target),
                    product=product,
                    previous_quantity=previous,
                    target_quantity=target,
                )
            )
            delta += product.price * (target - previous)
        if not changes:
            raise ValueError("the cart already matches the requested quantities")

        now = datetime.now(UTC)
        prepared = PreparedChangeSet(
            change_set_id=str(uuid4()),
            status=ChangeSetStatus.PREPARED,
            retailer=retailer,
            created_at=now,
            expires_at=now + timedelta(seconds=self._ttl_seconds),
            changes=changes,
            estimated_cart_delta=delta.quantize(Decimal("0.01")),
            expected_cart_total=(cart.total + delta).quantize(Decimal("0.01")),
            warnings=list(dict.fromkeys(warnings)),
        )
        await self._change_sets.create(
            prepared,
            cart_id=cart.cart_id,
            cart_snapshot_hash=cart.snapshot_hash(),
            requested=normalized_requests,
        )
        await self._audit.record(
            "prepare_cart_changes",
            "prepared",
            retailer=retailer,
            change_set_id=prepared.change_set_id,
            metadata={
                "changes": [
                    {
                        "product_id": change.product.retailer_product_id,
                        "previous_quantity": change.previous_quantity,
                        "target_quantity": change.target_quantity,
                    }
                    for change in changes
                ]
            },
        )
        return prepared

    async def apply_changes(self, change_set_id: str) -> ApplyResult:
        row = await self._change_sets.get_row(change_set_id)
        if row is None:
            raise ChangeSetNotFoundError(change_set_id)
        retailer = Retailer(row.retailer)
        async with self._apply_locks[retailer]:
            # Re-read after acquiring the lock: another proposal may have finished
            # while this caller waited.
            row = await self._change_sets.get_row(change_set_id)
            assert row is not None
            status = ChangeSetStatus(row.status)
            if status == ChangeSetStatus.APPLIED:
                prior = await self._change_sets.prior_result(change_set_id)
                if prior:
                    return prior
            if status != ChangeSetStatus.PREPARED:
                raise ChangeSetStateError(
                    f"change set cannot be applied from status {status.value}"
                )
            expires_at = (
                row.expires_at if row.expires_at.tzinfo else row.expires_at.replace(tzinfo=UTC)
            )
            if expires_at <= datetime.now(UTC):
                await self._change_sets.set_status(change_set_id, ChangeSetStatus.EXPIRED)
                await self._audit.record(
                    "apply_cart_changes",
                    "expired",
                    retailer=retailer,
                    change_set_id=change_set_id,
                )
                return ApplyResult(change_set_id=change_set_id, status=ChangeSetStatus.EXPIRED)

            if not await self._change_sets.claim_for_apply(change_set_id):
                prior = await self._change_sets.prior_result(change_set_id)
                if prior:
                    return prior
                raise ChangeSetStateError("change set is already being applied")

            prepared = await self._change_sets.get(change_set_id)
            assert prepared is not None
            return await self._apply_claimed(prepared, row.cart_id, row.cart_snapshot_hash)

    async def _apply_claimed(
        self,
        prepared: PreparedChangeSet,
        expected_cart_id: str,
        expected_snapshot_hash: str,
    ) -> ApplyResult:
        change_set_id = prepared.change_set_id
        adapter = self._adapters.get(prepared.retailer)
        try:
            cart = await adapter.get_cart()
            if cart.cart_id != expected_cart_id or cart.snapshot_hash() != expected_snapshot_hash:
                result = ApplyResult(
                    change_set_id=change_set_id,
                    status=ChangeSetStatus.REQUIRES_RECONFIRMATION,
                    reason="cart_changed_since_proposal",
                )
                await self._change_sets.set_status(
                    change_set_id,
                    result.status,
                    result=result,
                    reason=result.reason,
                )
                await self._audit.record(
                    "apply_cart_changes",
                    result.status.value,
                    retailer=prepared.retailer,
                    change_set_id=change_set_id,
                )
                return result

            for change in prepared.changes:
                if change.target_quantity <= change.previous_quantity:
                    # Reducing or removing must not be blocked by stock or price movement.
                    continue
                current = await adapter.get_product(change.product.retailer_product_id)
                if current.in_stock is False:
                    raise ChangeSetStateError(f"product is no longer available: {current.name}")
                if current.price <= 0 or change.product.price <= 0:
                    raise ChangeSetStateError(f"product price is unavailable: {current.name}")
                price_change = (
                    abs(current.price - change.product.price) / change.product.price * 100
                )
                if price_change > self._price_tolerance_percent:
                    result = ApplyResult(
                        change_set_id=change_set_id,
                        status=ChangeSetStatus.REQUIRES_RECONFIRMATION,
                        reason="price_changed_materially",
                    )
                    await self._change_sets.set_status(
                        change_set_id,
                        result.status,
                        result=result,
                        reason=result.reason,
                    )
                    await self._audit.record(
                        "apply_cart_changes",
                        result.status.value,
                        retailer=prepared.retailer,
                        change_set_id=change_set_id,
                        metadata={"reason": result.reason},
                    )
                    return result

            working_cart = cart
            for change in prepared.changes:
                existing = next(
                    (
                        line
                        for line in working_cart.items
                        if line.retailer_product_id == change.product.retailer_product_id
                    ),
                    None,
                )
                if existing:
                    working_cart = await adapter.set_quantity(
                        change.product.retailer_product_id, change.target_quantity
                    )
                else:
                    working_cart = await adapter.add_to_cart(
                        change.product.retailer_product_id, change.target_quantity
                    )
                await self._audit.record(
                    "apply_cart_change_item",
                    "written",
                    retailer=prepared.retailer,
                    change_set_id=change_set_id,
                    metadata={
                        "product_id": change.product.retailer_product_id,
                        "target_quantity": change.target_quantity,
                    },
                )

            final_cart = await adapter.get_cart()
            final_quantities = {
                line.retailer_product_id: line.quantity for line in final_cart.items
            }
            mismatches = [
                change.product.name
                for change in prepared.changes
                if final_quantities.get(change.product.retailer_product_id, 0)
                != change.target_quantity
            ]
            if mismatches:
                raise ChangeSetStateError("cart write could not be verified")

            result = ApplyResult(
                change_set_id=change_set_id,
                status=ChangeSetStatus.APPLIED,
                cart_total=final_cart.total,
            )
            await self._change_sets.set_status(
                change_set_id, ChangeSetStatus.APPLIED, result=result
            )
            await self._audit.record(
                "apply_cart_changes",
                "applied",
                retailer=prepared.retailer,
                change_set_id=change_set_id,
                metadata={
                    "changes": [
                        {
                            "product_id": change.product.retailer_product_id,
                            "previous_quantity": change.previous_quantity,
                            "target_quantity": change.target_quantity,
                        }
                        for change in prepared.changes
                    ]
                },
            )
            return result
        except Exception as exc:
            await self._change_sets.set_status(
                change_set_id,
                ChangeSetStatus.FAILED,
                reason=type(exc).__name__,
            )
            await self._audit.record(
                "apply_cart_changes",
                "failed",
                retailer=prepared.retailer,
                change_set_id=change_set_id,
                metadata={"error_type": type(exc).__name__},
            )
            raise

    async def discard_changes(self, change_set_id: str) -> ChangeSetStatus:
        try:
            status = await self._change_sets.discard(change_set_id)
        except KeyError as exc:
            raise ChangeSetNotFoundError(change_set_id) from exc
        await self._audit.record("discard_cart_changes", status.value, change_set_id=change_set_id)
        return status


def _action(previous: int, target: int) -> ChangeAction:
    if target == 0:
        return ChangeAction.REMOVE
    return ChangeAction.INCREASE if target > previous else ChangeAction.REDUCE
