from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update

from grocery_mcp.domain.change_sets import (
    ApplyResult,
    ChangeSetStatus,
    PreparedChange,
    PreparedChangeSet,
    RequestedItem,
)
from grocery_mcp.domain.models import Retailer
from grocery_mcp.domain.preferences import CanonicalItem, ItemPreference, RetailerItemMapping
from grocery_mcp.logging import redact
from grocery_mcp.storage.db import (
    AuditEventRow,
    CanonicalItemRow,
    ChangeSetRow,
    Database,
    RetailerItemMappingRow,
    RetailerLocationRow,
    RetailerSessionRow,
    utcnow,
)
from grocery_mcp.storage.encryption import SecretBox


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class EncryptedRetailerStateStore:
    def __init__(self, database: Database, secret_box: SecretBox) -> None:
        self._db = database
        self._secret_box = secret_box

    async def load_session(self, retailer: Retailer | str) -> dict[str, Any] | None:
        key = str(retailer)
        async with self._db.session() as session:
            row = await session.get(RetailerSessionRow, key)
            return self._secret_box.decrypt_json(row.encrypted_payload) if row else None

    async def save_session(self, retailer: Retailer | str, payload: dict[str, Any]) -> None:
        key = str(retailer)
        encrypted = self._secret_box.encrypt_json(payload)
        async with self._db.session() as session, session.begin():
            row = await session.get(RetailerSessionRow, key)
            if row:
                row.encrypted_payload = encrypted
                row.updated_at = utcnow()
            else:
                session.add(RetailerSessionRow(retailer=key, encrypted_payload=encrypted))

    async def clear_session(self, retailer: Retailer | str) -> None:
        key = str(retailer)
        async with self._db.session() as session, session.begin():
            row = await session.get(RetailerSessionRow, key)
            if row:
                await session.delete(row)

    async def load_location(self, retailer: Retailer | str) -> dict[str, Any] | None:
        key = str(retailer)
        async with self._db.session() as session:
            row = await session.get(RetailerLocationRow, key)
            return self._secret_box.decrypt_json(row.encrypted_payload) if row else None

    async def save_location(self, retailer: Retailer | str, payload: dict[str, Any]) -> None:
        key = str(retailer)
        encrypted = self._secret_box.encrypt_json(payload)
        async with self._db.session() as session, session.begin():
            row = await session.get(RetailerLocationRow, key)
            if row:
                row.encrypted_payload = encrypted
                row.updated_at = utcnow()
            else:
                session.add(RetailerLocationRow(retailer=key, encrypted_payload=encrypted))


class PreferenceRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def upsert_item(self, item: CanonicalItem) -> None:
        async with self._db.session() as session, session.begin():
            row = await session.get(CanonicalItemRow, item.key)
            values = {
                "display_name": item.display_name,
                "default_quantity": item.default_quantity,
                "aliases_json": json.dumps(item.aliases),
                "preferences_json": json.dumps(
                    [preference.model_dump(mode="json") for preference in item.preferences]
                ),
            }
            if row:
                for key, value in values.items():
                    setattr(row, key, value)
                row.updated_at = utcnow()
            else:
                session.add(CanonicalItemRow(key=item.key, **values))

    async def map_product(self, mapping: RetailerItemMapping) -> None:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                select(RetailerItemMappingRow).where(
                    RetailerItemMappingRow.canonical_key == mapping.canonical_key,
                    RetailerItemMappingRow.retailer == mapping.retailer.value,
                )
            )
            row = result.scalar_one_or_none()
            if row:
                row.retailer_product_id = mapping.retailer_product_id
                row.product_name = mapping.product_name
            else:
                session.add(
                    RetailerItemMappingRow(
                        canonical_key=mapping.canonical_key,
                        retailer=mapping.retailer.value,
                        retailer_product_id=mapping.retailer_product_id,
                        product_name=mapping.product_name,
                    )
                )

    async def resolve(
        self, value: str, retailer: Retailer
    ) -> tuple[CanonicalItem, RetailerItemMapping | None] | None:
        normalized = value.strip().lower().replace(" ", "_")
        async with self._db.session() as session:
            result = await session.execute(select(CanonicalItemRow))
            rows = result.scalars().all()
            item_row = next(
                (
                    row
                    for row in rows
                    if row.key == normalized
                    or value.strip().lower()
                    in {alias.lower() for alias in json.loads(row.aliases_json)}
                ),
                None,
            )
            if item_row is None:
                return None
            item = CanonicalItem(
                key=item_row.key,
                display_name=item_row.display_name,
                default_quantity=item_row.default_quantity,
                aliases=json.loads(item_row.aliases_json),
                preferences=[
                    ItemPreference.model_validate(p) for p in json.loads(item_row.preferences_json)
                ],
            )
            mapping_result = await session.execute(
                select(RetailerItemMappingRow).where(
                    RetailerItemMappingRow.canonical_key == item.key,
                    RetailerItemMappingRow.retailer == retailer.value,
                )
            )
            mapping_row = mapping_result.scalar_one_or_none()
            mapping = (
                RetailerItemMapping(
                    canonical_key=mapping_row.canonical_key,
                    retailer=Retailer(mapping_row.retailer),
                    retailer_product_id=mapping_row.retailer_product_id,
                    product_name=mapping_row.product_name,
                )
                if mapping_row
                else None
            )
            return item, mapping

    async def list_items(self) -> list[CanonicalItem]:
        async with self._db.session() as session:
            result = await session.execute(select(CanonicalItemRow).order_by(CanonicalItemRow.key))
            return [
                CanonicalItem(
                    key=row.key,
                    display_name=row.display_name,
                    default_quantity=row.default_quantity,
                    aliases=json.loads(row.aliases_json),
                    preferences=[
                        ItemPreference.model_validate(p) for p in json.loads(row.preferences_json)
                    ],
                )
                for row in result.scalars()
            ]


class ChangeSetRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def create(
        self,
        change_set: PreparedChangeSet,
        *,
        cart_id: str,
        cart_snapshot_hash: str,
        requested: list[RequestedItem],
    ) -> None:
        async with self._db.session() as session, session.begin():
            session.add(
                ChangeSetRow(
                    id=change_set.change_set_id,
                    retailer=change_set.retailer.value,
                    status=change_set.status.value,
                    created_at=change_set.created_at,
                    expires_at=change_set.expires_at,
                    cart_id=cart_id,
                    cart_snapshot_hash=cart_snapshot_hash,
                    requested_json=json.dumps([item.model_dump(mode="json") for item in requested]),
                    changes_json=json.dumps(
                        [
                            change.model_dump(mode="json", exclude_computed_fields=True)
                            for change in change_set.changes
                        ]
                    ),
                    estimated_delta=str(change_set.estimated_cart_delta),
                    expected_total=str(change_set.expected_cart_total),
                    warnings_json=json.dumps(change_set.warnings),
                )
            )

    async def get_row(self, change_set_id: str) -> ChangeSetRow | None:
        async with self._db.session() as session:
            return await session.get(ChangeSetRow, change_set_id)

    async def get(self, change_set_id: str) -> PreparedChangeSet | None:
        row = await self.get_row(change_set_id)
        if row is None:
            return None
        return PreparedChangeSet(
            change_set_id=row.id,
            status=ChangeSetStatus(row.status),
            retailer=Retailer(row.retailer),
            created_at=_aware(row.created_at),
            expires_at=_aware(row.expires_at),
            changes=[PreparedChange.model_validate(item) for item in json.loads(row.changes_json)],
            estimated_cart_delta=Decimal(row.estimated_delta),
            expected_cart_total=Decimal(row.expected_total),
            warnings=json.loads(row.warnings_json),
        )

    async def claim_for_apply(self, change_set_id: str) -> bool:
        async with self._db.session() as session, session.begin():
            result = await session.execute(
                update(ChangeSetRow)
                .where(
                    ChangeSetRow.id == change_set_id,
                    ChangeSetRow.status == ChangeSetStatus.PREPARED.value,
                )
                .values(status=ChangeSetStatus.APPLYING.value, updated_at=utcnow())
            )
            return result.rowcount == 1

    async def set_status(
        self,
        change_set_id: str,
        status: ChangeSetStatus,
        *,
        result: ApplyResult | None = None,
        reason: str | None = None,
    ) -> None:
        async with self._db.session() as session, session.begin():
            row = await session.get(ChangeSetRow, change_set_id)
            if row is None:
                raise KeyError(change_set_id)
            row.status = status.value
            row.updated_at = utcnow()
            row.result_json = result.model_dump_json() if result else row.result_json
            if reason is not None:
                row.failure_reason = reason

    async def prior_result(self, change_set_id: str) -> ApplyResult | None:
        row = await self.get_row(change_set_id)
        return ApplyResult.model_validate_json(row.result_json) if row and row.result_json else None

    async def discard(self, change_set_id: str) -> ChangeSetStatus:
        async with self._db.session() as session, session.begin():
            row = await session.get(ChangeSetRow, change_set_id)
            if row is None:
                raise KeyError(change_set_id)
            status = ChangeSetStatus(row.status)
            if status == ChangeSetStatus.PREPARED:
                row.status = ChangeSetStatus.DISCARDED.value
                row.updated_at = utcnow()
                return ChangeSetStatus.DISCARDED
            return status


class AuditRepository:
    def __init__(self, database: Database) -> None:
        self._db = database

    async def record(
        self,
        action: str,
        outcome: str,
        *,
        retailer: Retailer | None = None,
        change_set_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        safe_metadata = redact(metadata or {})
        async with self._db.session() as session, session.begin():
            session.add(
                AuditEventRow(
                    action=action,
                    retailer=retailer.value if retailer else None,
                    change_set_id=change_set_id,
                    outcome=outcome,
                    metadata_json=json.dumps(safe_metadata, default=str),
                )
            )
