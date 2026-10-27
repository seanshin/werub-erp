"""BI reporting 마트 확장 P0-d — 자금수지·채권채무 aging·기능별 손익.

기획서 docs/01-plan/features/bi-enhancements.plan.md §0. 기존 집계(settlement.cash_flow·aging,
statements.income_statement) 마트화 — Grafana 대시보드·Metabase 질문 확장 인에이블러. 집계·PII 없음.
"""
import sqlalchemy as sa
from alembic import op

revision = "0148"
down_revision = "0147"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_cashflow_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("inflow", sa.Numeric(18, 2)),
        sa.Column("outflow", sa.Numeric(18, 2)),
        sa.Column("net", sa.Numeric(18, 2)),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )
    op.create_table(
        "mart_aging_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("kind", sa.String(2), primary_key=True),
        sa.Column("bucket_current", sa.Numeric(18, 2)),
        sa.Column("bucket_1_30", sa.Numeric(18, 2)),
        sa.Column("bucket_31_60", sa.Numeric(18, 2)),
        sa.Column("bucket_61_90", sa.Numeric(18, 2)),
        sa.Column("bucket_90plus", sa.Numeric(18, 2)),
        sa.Column("total", sa.Numeric(18, 2)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )
    op.create_table(
        "mart_income_function_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("total_revenue", sa.Numeric(18, 2)),
        sa.Column("total_expense", sa.Numeric(18, 2)),
        sa.Column("net_income", sa.Numeric(18, 2)),
        sa.Column("cogs", sa.Numeric(18, 2)),
        sa.Column("sga", sa.Numeric(18, 2)),
        sa.Column("other", sa.Numeric(18, 2)),
        sa.Column("gross_profit", sa.Numeric(18, 2)),
        sa.Column("operating_income", sa.Numeric(18, 2)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_income_function_monthly", schema="reporting")
    op.drop_table("mart_aging_monthly", schema="reporting")
    op.drop_table("mart_cashflow_monthly", schema="reporting")
