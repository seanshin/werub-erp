"""BI reporting 마트 확장 잔여 2종 — 자금수지 예측·연간 재무비율.

기획서 bi-enhancements §0 잔여(cash_forecast·ratios_annual). settlement.cash_forecast·
statements.financial_ratios 마트화. 집계·PII 없음. erp_bi는 ALTER DEFAULT PRIVILEGES로 자동 SELECT.
"""
import sqlalchemy as sa
from alembic import op

revision = "0150"
down_revision = "0149"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_cash_forecast",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("bucket_idx", sa.Integer, primary_key=True),
        sa.Column("label", sa.String(16)),
        sa.Column("inflow", sa.Numeric(18, 2)),
        sa.Column("outflow", sa.Numeric(18, 2)),
        sa.Column("net", sa.Numeric(18, 2)),
        sa.Column("balance", sa.Numeric(18, 2)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )
    op.create_table(
        "mart_ratios_annual",
        sa.Column("year", sa.Integer, primary_key=True),
        sa.Column("current_ratio", sa.Numeric(10, 4)),
        sa.Column("debt_ratio", sa.Numeric(10, 4)),
        sa.Column("equity_ratio", sa.Numeric(10, 4)),
        sa.Column("operating_margin", sa.Numeric(10, 4)),
        sa.Column("labor_cost_ratio", sa.Numeric(10, 4)),
        sa.Column("material_cost_ratio", sa.Numeric(10, 4)),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_ratios_annual", schema="reporting")
    op.drop_table("mart_cash_forecast", schema="reporting")
