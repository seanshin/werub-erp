"""진료과별 손익 월간 마트 (BI P0-b).

기획서 docs/01-plan/features/bi-tool-integration.plan.md §8 P0-b. exec KPI 마트(월별 톱라인)에
부서 차원 드릴다운을 더한다 — segment_report() 기반 reporting.mart_dept_pl_monthly(집계·PII 없음).
erp_bi 읽기전용 롤은 ALTER DEFAULT PRIVILEGES로 신규 reporting 테이블 자동 SELECT(런북 §실행 2).
"""
import sqlalchemy as sa
from alembic import op

revision = "0146"
down_revision = "0145"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_dept_pl_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("dept_code", sa.String(32), primary_key=True),
        sa.Column("department", sa.String(80)),
        sa.Column("revenue", sa.Numeric(18, 2)),
        sa.Column("profit", sa.Numeric(18, 2)),
        sa.Column("assets", sa.Numeric(18, 2)),
        sa.Column("reportable", sa.Boolean),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_dept_pl_monthly", schema="reporting")
