"""0215 — 부대사업 수익(의료외수익·내부운영 갭 감사 Tier1): 항목 마스터 + 수납.

audit 발견: 주차·임대·비보험검진 등 의료외수익 계정/모델 전무(4110·4120·46xx만)·병원회계기준상
의료외수익 별도 분류 요구. 범용 설정형(기관이 실제 사업만 활성화):
- 계정 4130 의료외수익·2135 부가세예수금(부대사업 원칙 과세).
- finance.ancillary_revenue_items: 항목 마스터(카테고리·과세여부·기본가).
- finance.ancillary_revenues: 수납(순액 4130 + 과세 시 2135 분리).

Revision ID: 0215
"""
import sqlalchemy as sa
from alembic import op

revision = "0215"
down_revision = "0214"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('4130','의료외수익','REVENUE'),
            ('2135','부가세예수금','LIABILITY')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "ancillary_revenue_items",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("category", sa.String(12), nullable=False, server_default="OTHER", index=True),
        sa.Column("account_code", sa.String(20), nullable=False, server_default="4130"),
        sa.Column("taxable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("default_price", sa.Numeric(14, 0), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint(
            "category IN ('PARKING','RENTAL','CAFETERIA','CHECKUP','FUNERAL','OTHER')",
            name="ck_ancrev_cat"),
        schema="finance",
    )
    op.create_table(
        "ancillary_revenues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("item_id", sa.BigInteger(),
                  sa.ForeignKey("finance.ancillary_revenue_items.id"), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("net_amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("vat_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("method", sa.String(10), nullable=False, server_default="CASH"),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False, server_default="1"),
        sa.Column("occurred_on", sa.Date(), nullable=False, index=True),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("voucher_ref", sa.String(64), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("ancillary_revenues", schema="finance")
    op.drop_table("ancillary_revenue_items", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('4130','2135')")
