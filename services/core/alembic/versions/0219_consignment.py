"""0219 — 위탁재고(Consignment·내부운영 갭 감사 Tier2): 소유권 미이전 보관·사용 시점 매입.

audit 발견: 고가 임플란트·스텐트를 사용 후 청구·정산하는 위탁재고(소유권 미이전) 부재. 적재는 회계
없음(미소유)·사용 시 DR 재료비(5410)/CR 미지급금(2110) 매입 인식. 일반 Lot(소유재고)과 분리.

Revision ID: 0219
"""
import sqlalchemy as sa
from alembic import op

revision = "0219"
down_revision = "0218"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consignment_stock",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False,
                  index=True),
        sa.Column("lot_no", sa.String(40), nullable=False),
        sa.Column("expiry_on", sa.Date(), nullable=True),
        sa.Column("placed_qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("qty_on_hand", sa.Numeric(12, 2), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("placed_on", sa.Date(), nullable=False, index=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE", index=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("status IN ('ACTIVE','DEPLETED','RETURNED')", name="ck_consign_status"),
        sa.CheckConstraint("qty_on_hand >= 0", name="ck_consign_nonneg"),
        schema="scm",
    )
    op.create_table(
        "consignment_consumptions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("consignment_id", sa.BigInteger(),
                  sa.ForeignKey("scm.consignment_stock.id"), nullable=False, index=True),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount", sa.Numeric(16, 0), nullable=False),
        sa.Column("consumed_on", sa.Date(), nullable=False, index=True),
        sa.Column("chart_no", sa.String(20), nullable=True, index=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("payable_ref", sa.String(64), nullable=True),
        sa.Column("actor", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        schema="scm",
    )


def downgrade() -> None:
    op.drop_table("consignment_consumptions", schema="scm")
    op.drop_table("consignment_stock", schema="scm")
