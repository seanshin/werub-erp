"""RFQ/견적/단가계약 — 소싱 상류 도메인 (남은 항목 2/4).

RFQ→견적(quotes)→낙찰→단가계약(price_agreements). PO가 단가계약 참조 시 계약단가 자동 적용.
"""
from alembic import op
import sqlalchemy as sa

revision = "0143"
down_revision = "0142"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rfqs",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("category", sa.String(12)),
        sa.Column("due_on", sa.Date()),
        sa.Column("status", sa.String(10), nullable=False, server_default="DRAFT"),
        sa.Column("awarded_quote_id", sa.BigInteger()),
        sa.Column("note", sa.Text()),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('DRAFT','OPEN','CLOSED','AWARDED','CANCELED')",
                           name="ck_rfq_status"),
        schema="scm",
    )
    op.create_table(
        "rfq_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("rfq_id", sa.BigInteger(), sa.ForeignKey("scm.rfqs.id"), nullable=False),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("spec", sa.String(200)),
        schema="scm",
    )
    op.create_index("ix_rfqline_rfq", "rfq_lines", ["rfq_id"], schema="scm")
    op.create_table(
        "quotes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("rfq_id", sa.BigInteger(), sa.ForeignKey("scm.rfqs.id"), nullable=False),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("scm.suppliers.id"),
                  nullable=False),
        sa.Column("total", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("status", sa.String(10), nullable=False, server_default="SUBMITTED"),
        sa.Column("submitted_on", sa.Date()),
        sa.Column("note", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('SUBMITTED','AWARDED','REJECTED')", name="ck_quote_status"),
        schema="scm",
    )
    op.create_index("ix_quote_rfq", "quotes", ["rfq_id"], schema="scm")
    op.create_table(
        "quote_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("quote_id", sa.BigInteger(), sa.ForeignKey("scm.quotes.id"), nullable=False),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("lead_time_days", sa.Integer()),
        schema="scm",
    )
    op.create_index("ix_quoteline_quote", "quote_lines", ["quote_id"], schema="scm")
    op.create_table(
        "price_agreements",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("scm.suppliers.id"),
                  nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("rfq_id", sa.BigInteger()),
        sa.Column("valid_from", sa.Date(), nullable=False),
        sa.Column("valid_to", sa.Date(), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('ACTIVE','EXPIRED','TERMINATED')", name="ck_pa_status"),
        schema="scm",
    )
    op.create_index("ix_pa_supplier", "price_agreements", ["supplier_id"], schema="scm")
    op.create_table(
        "price_agreement_lines",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("agreement_id", sa.BigInteger(),
                  sa.ForeignKey("scm.price_agreements.id"), nullable=False),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.UniqueConstraint("agreement_id", "item_id", name="uq_paline_item"),
        schema="scm",
    )
    op.create_index("ix_paline_agreement", "price_agreement_lines", ["agreement_id"], schema="scm")


def downgrade() -> None:
    for t in ("price_agreement_lines", "price_agreements", "quote_lines", "quotes",
              "rfq_lines", "rfqs"):
        op.drop_table(t, schema="scm")
