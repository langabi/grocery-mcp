"""Initial grocery state schema."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retailer_sessions",
        sa.Column("retailer", sa.String(32), primary_key=True),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "retailer_locations",
        sa.Column("retailer", sa.String(32), primary_key=True),
        sa.Column("encrypted_payload", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "canonical_items",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("display_name", sa.String(256), nullable=False),
        sa.Column("default_quantity", sa.Integer(), nullable=False),
        sa.Column("aliases_json", sa.Text(), nullable=False),
        sa.Column("preferences_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "retailer_item_mappings",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "canonical_key",
            sa.String(128),
            sa.ForeignKey("canonical_items.key"),
            nullable=False,
        ),
        sa.Column("retailer", sa.String(32), nullable=False),
        sa.Column("retailer_product_id", sa.String(256), nullable=False),
        sa.Column("product_name", sa.String(512), nullable=False),
        sa.UniqueConstraint("canonical_key", "retailer"),
    )
    op.create_table(
        "change_sets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("retailer", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cart_id", sa.String(256), nullable=False),
        sa.Column("cart_snapshot_hash", sa.String(64), nullable=False),
        sa.Column("requested_json", sa.Text(), nullable=False),
        sa.Column("changes_json", sa.Text(), nullable=False),
        sa.Column("estimated_delta", sa.String(64), nullable=False),
        sa.Column("expected_total", sa.String(64), nullable=False),
        sa.Column("warnings_json", sa.Text(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.String(256), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_change_sets_retailer", "change_sets", ["retailer"])
    op.create_index("ix_change_sets_status", "change_sets", ["status"])
    op.create_index("ix_change_sets_expires_at", "change_sets", ["expires_at"])
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("action", sa.String(128), nullable=False),
        sa.Column("retailer", sa.String(32), nullable=True),
        sa.Column("change_set_id", sa.String(36), nullable=True),
        sa.Column("outcome", sa.String(64), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_audit_events_action", "audit_events", ["action"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("change_sets")
    op.drop_table("retailer_item_mappings")
    op.drop_table("canonical_items")
    op.drop_table("retailer_locations")
    op.drop_table("retailer_sessions")
