"""0238 — LIS 검사 청구 인바운드 (E-3 A: LIS 수량·ERP 산정).

lis_test_edi_map(원내 검사코드↔EDI 수가코드)·lis_billing_claims(오더 단위 캡처·멱등)·
lis_billing_claim_items. LIS→ERP 청구 연동 개통.

Revision ID: 0238
"""
import sqlalchemy as sa
from alembic import op

revision = "0238"
down_revision = "0237"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lis_test_edi_map",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("test_code", sa.String(40), nullable=False),
        sa.Column("edi_code", sa.String(20), nullable=False),
        sa.Column("name", sa.String(120), nullable=True),
        sa.Column("active", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("test_code", name="uq_lis_test_edi_map"),
        schema="integration",
    )
    op.create_table(
        "lis_billing_claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("lis_billing_id", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("chart_no", sa.String(20), nullable=True),
        sa.Column("visit_id", sa.String(64), nullable=True),
        sa.Column("performed_on", sa.Date(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="CAPTURED"),
        sa.Column("item_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Numeric(14, 0), nullable=True),
        sa.Column("mapped", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("lis_billing_id", name="uq_lis_billing_id"),
        sa.CheckConstraint("status IN ('CAPTURED','CANCELLED')", name="ck_lis_claim_status"),
        schema="integration",
    )
    op.create_table(
        "lis_billing_claim_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("claim_id", sa.BigInteger(),
                  sa.ForeignKey("integration.lis_billing_claims.id"), nullable=False, index=True),
        sa.Column("test_code", sa.String(40), nullable=False),
        sa.Column("test_name", sa.String(120), nullable=True),
        sa.Column("quantity", sa.Numeric(10, 2), nullable=False, server_default="1"),
        sa.Column("specimen", sa.String(20), nullable=True),
        sa.Column("result_status", sa.String(12), nullable=True),
        sa.Column("edi_code", sa.String(20), nullable=True),
        sa.Column("unit_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=True),
        schema="integration",
    )


def downgrade() -> None:
    op.drop_table("lis_billing_claim_items", schema="integration")
    op.drop_table("lis_billing_claims", schema="integration")
    op.drop_table("lis_test_edi_map", schema="integration")
