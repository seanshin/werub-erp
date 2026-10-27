"""reporting 스키마 + 경영진 KPI 마트 (BI P0).

기획서 docs/01-plan/features/bi-tool-integration.plan.md §8 P0. 운영 스키마와 분리된 reporting
스키마 + mart_exec_kpi_monthly(집계·PII 없음). 읽기전용 롤 erp_bi 생성·GRANT는 슈퍼유저 작업이라
마이그가 아니라 런북(docs/05-operations/bi-reporting-role.runbook.md)에서 수행한다 — erp_app은
CREATE ROLE 권한이 없다.
"""
import sqlalchemy as sa
from alembic import op

revision = "0145"
down_revision = "0144"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS reporting")
    op.create_table(
        "mart_exec_kpi_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("revenue", sa.Numeric(18, 2)),
        sa.Column("net_income", sa.Numeric(18, 2)),
        sa.Column("total_assets", sa.Numeric(18, 2)),
        sa.Column("total_liabilities", sa.Numeric(18, 2)),
        sa.Column("ar_balance", sa.Numeric(18, 2)),
        sa.Column("headcount", sa.Integer),
        sa.Column("labor_cost", sa.Numeric(18, 2)),
        sa.Column("turnover_pct", sa.Numeric(6, 2)),
        sa.Column("patients", sa.Integer),
        sa.Column("receptions", sa.Integer),
        sa.Column("deduction_loss", sa.Numeric(18, 2)),
        sa.Column("current_ratio", sa.Numeric(10, 2)),
        sa.Column("debt_ratio_pct", sa.Numeric(8, 2)),
        sa.Column("operating_margin_pct", sa.Numeric(8, 2)),
        sa.Column("inventory_value", sa.Numeric(18, 2)),
        sa.Column("asset_book_value", sa.Numeric(18, 2)),
        sa.Column("equity", sa.Numeric(18, 2)),
        sa.Column("bs_balanced", sa.Boolean),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_exec_kpi_monthly", schema="reporting")
    op.execute("DROP SCHEMA IF EXISTS reporting")
