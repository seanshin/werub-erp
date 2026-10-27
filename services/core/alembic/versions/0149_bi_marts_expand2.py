"""BI reporting 마트 확장 P0-d(2) — 검진권·진료과 이직·재고.

기획서 bi-enhancements §0/§A 잔여. analytics.voucher_overview·hr_turnover, scm.valuation/shortage/turnover
마트화. 집계·PII 없음. erp_bi는 ALTER DEFAULT PRIVILEGES로 자동 SELECT.
"""
import sqlalchemy as sa
from alembic import op

revision = "0149"
down_revision = "0148"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_voucher_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("issued_amount", sa.Numeric(18, 2)),
        sa.Column("redeemed_amount", sa.Numeric(18, 2)),
        sa.Column("refunded_amount", sa.Numeric(18, 2)),
        sa.Column("settled_amount", sa.Numeric(18, 2)),
        sa.Column("unredeemed_krw", sa.Integer),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )
    op.create_table(
        "mart_hr_turnover_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("department", sa.String(80), primary_key=True),
        sa.Column("active", sa.Integer),
        sa.Column("left", sa.Integer),
        sa.Column("rate", sa.Numeric(6, 2)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )
    op.create_table(
        "mart_inventory_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("total_value", sa.Numeric(18, 2)),
        sa.Column("shortage_count", sa.Integer),
        sa.Column("dead_stock_count", sa.Integer),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_inventory_monthly", schema="reporting")
    op.drop_table("mart_hr_turnover_monthly", schema="reporting")
    op.drop_table("mart_voucher_monthly", schema="reporting")
