"""0186 — IT 비용(ITAM) 월간 BI 마트.

reporting.mart_it_cost_monthly — itam.cost_service 집계(OPEX·예산·GL 전기 현황) 평탄화.
BI(Grafana/Metabase)가 IT 비용 추세·예산 대비·전기 진척을 읽는다(집계·PII 없음).
erp_bi 읽기전용 롤은 ALTER DEFAULT PRIVILEGES로 신규 reporting 테이블 자동 SELECT(런북).

Revision ID: 0186
"""
import sqlalchemy as sa
from alembic import op

revision = "0186"
down_revision = "0185"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "mart_it_cost_monthly",
        sa.Column("yyyymm", sa.String(7), primary_key=True),
        sa.Column("total_krw", sa.Numeric(18, 0)),
        sa.Column("budget_krw", sa.Numeric(18, 0)),
        sa.Column("over_budget", sa.Boolean),
        sa.Column("posted_krw", sa.Numeric(18, 0)),
        sa.Column("unposted_krw", sa.Numeric(18, 0)),
        sa.Column("unposted_count", sa.Integer),
        sa.Column("service_count", sa.Integer),
        sa.Column("payload", sa.JSON),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="reporting",
    )


def downgrade() -> None:
    op.drop_table("mart_it_cost_monthly", schema="reporting")
