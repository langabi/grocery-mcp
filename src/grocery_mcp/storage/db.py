from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import anyio
from alembic import command
from alembic.config import Config
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def utcnow() -> datetime:
    return datetime.now(UTC)


def alembic_config(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    config.set_main_option("sqlalchemy.url", url)
    return config


class Base(DeclarativeBase):
    pass


class RetailerSessionRow(Base):
    __tablename__ = "retailer_sessions"

    retailer: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RetailerLocationRow(Base):
    __tablename__ = "retailer_locations"

    retailer: Mapped[str] = mapped_column(String(32), primary_key=True)
    encrypted_payload: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class CanonicalItemRow(Base):
    __tablename__ = "canonical_items"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(256))
    default_quantity: Mapped[int] = mapped_column(Integer, default=1)
    aliases_json: Mapped[str] = mapped_column(Text, default="[]")
    preferences_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RetailerItemMappingRow(Base):
    __tablename__ = "retailer_item_mappings"
    __table_args__ = (UniqueConstraint("canonical_key", "retailer"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    canonical_key: Mapped[str] = mapped_column(ForeignKey("canonical_items.key"))
    retailer: Mapped[str] = mapped_column(String(32))
    retailer_product_id: Mapped[str] = mapped_column(String(256))
    product_name: Mapped[str] = mapped_column(String(512))


class ChangeSetRow(Base):
    __tablename__ = "change_sets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    retailer: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    cart_id: Mapped[str] = mapped_column(String(256))
    cart_snapshot_hash: Mapped[str] = mapped_column(String(64))
    requested_json: Mapped[str] = mapped_column(Text)
    changes_json: Mapped[str] = mapped_column(Text)
    estimated_delta: Mapped[str] = mapped_column(String(64))
    expected_total: Mapped[str] = mapped_column(String(64))
    warnings_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(256), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEventRow(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    action: Mapped[str] = mapped_column(String(128), index=True)
    retailer: Mapped[str | None] = mapped_column(String(32), nullable=True)
    change_set_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    outcome: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[str] = mapped_column(Text, default="{}")


class Database:
    def __init__(self, url: str) -> None:
        self.url = url
        self.engine: AsyncEngine = create_async_engine(url)
        if url.startswith("sqlite"):

            @event.listens_for(self.engine.sync_engine, "connect")
            def configure_sqlite(connection, _record) -> None:  # type: ignore[no-untyped-def]
                cursor = connection.cursor()
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.close()

        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def initialise(self) -> None:
        raw_path = make_url(self.url).database if self.url.startswith("sqlite") else None
        if raw_path and raw_path != ":memory:":
            directory = Path(raw_path).parent
            await anyio.to_thread.run_sync(
                lambda: directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            )
            await anyio.to_thread.run_sync(directory.chmod, 0o700)
        await anyio.to_thread.run_sync(self._migrate)
        if raw_path and raw_path != ":memory:":
            await anyio.to_thread.run_sync(Path(raw_path).chmod, 0o600)

    def _migrate(self) -> None:
        """Alembic is the only thing that creates or alters schema, in every environment."""
        command.upgrade(alembic_config(self.url), "head")

    def session(self) -> AsyncSession:
        return self.sessions()

    async def close(self) -> None:
        await self.engine.dispose()
