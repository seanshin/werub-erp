"""월간 감사·이상탐지 마트 (BI P0-c).

기획서 docs/01-plan/features/bi-tool-integration.plan.md §8 P0-c. exec KPI/dept 마트에 없는
차원(감사·이상탐지) — audit_service.summary()(Benford·중복·비정상) 기반 reporting.mart_audit_flags_monthly.
집계·PII 없음(건수·점수·금액합). erp_bi는 ALTER DEFAULT PRIVILEGES로 자동 SELECT(롤 런북).
"""
import sqlalchemy as sa
from alembic import op

revision = "0147"
down_revision = "0146"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_audit_flags_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("benford_verdict", sa.String(16)),
        sa.Column("benford_mad", sa.Numeric(10, 5)),
        sa.Column("benford_sample_size", sa.Integer),
        sa.Column("duplicate_clusters", sa.Integer),
        sa.Column("duplicate_open", sa.Integer),
        sa.Column("duplicate_amount", sa.Numeric(18, 2)),
        sa.Column("abnormal_flagged", sa.Integer),
        sa.Column("abnormal_open", sa.Integer),
        sa.Column("open_total", sa.Integer),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_audit_flags_monthly", schema="reporting")
