"""0218 — 고정자산 부서이동·실사(내부운영 갭 감사 Tier2).

audit 발견: FixedAsset.department_id UPDATE에 감사추적·이력 테이블 없음·물리 실사(재물조사) 미지원.
보완: asset_transfers(부서간 이동 이력)·asset_inventories/asset_inventory_lines(실사 세션·대사).

Revision ID: 0218
"""
import sqlalchemy as sa
from alembic import op

revision = "0218"
down_revision = "0217"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "asset_transfers",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("asset_id", sa.BigInteger(),
                  sa.ForeignKey("finance.fixed_assets.id"), nullable=False, index=True),
        sa.Column("from_dept", sa.BigInteger(), nullable=True),
        sa.Column("to_dept", sa.BigInteger(), nullable=True),
        sa.Column("transferred_on", sa.Date(), nullable=False, index=True),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("actor", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        schema="finance",
    )
    op.create_table(
        "asset_inventories",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("conducted_on", sa.Date(), nullable=False, index=True),
        sa.Column("status", sa.String(8), nullable=False, server_default="OPEN", index=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_assetinv_status"),
        schema="finance",
    )
    op.create_table(
        "asset_inventory_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("inventory_id", sa.BigInteger(),
                  sa.ForeignKey("finance.asset_inventories.id"), nullable=False, index=True),
        sa.Column("asset_id", sa.BigInteger(),
                  sa.ForeignKey("finance.fixed_assets.id"), nullable=False, index=True),
        sa.Column("expected_dept", sa.BigInteger(), nullable=True),
        sa.Column("found_status", sa.String(12), nullable=False, server_default="NOT_CHECKED"),
        sa.Column("actual_dept", sa.BigInteger(), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("checked_by", sa.BigInteger(), nullable=True),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "found_status IN ('NOT_CHECKED','FOUND','NOT_FOUND','RELOCATED')",
            name="ck_assetinvline_status"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("asset_inventory_lines", schema="finance")
    op.drop_table("asset_inventories", schema="finance")
    op.drop_table("asset_transfers", schema="finance")
