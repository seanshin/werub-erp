"""0179 — ITAM R4: IT 비용 예산(it_budgets).

월/연 예산을 코스트센터(부서)별·전체로 설정해 실적(service_costs) 대비 초과를 감지(F-6 예산).
회계 예산(finance)과 별개인 IT 운영 예산 — 전표 전기·BI 마트는 별도(백로그).

Revision ID: 0179
"""
import sqlalchemy as sa
from alembic import op

revision = "0179"
down_revision = "0178"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "it_budgets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(7), nullable=False),          # yyyy(연) 또는 yyyy-mm(월)
        sa.Column("cost_center", sa.String(40)),                    # NULL = 전체(기관 IT 총예산)
        sa.Column("amount_krw", sa.Numeric(16, 0), nullable=False),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(),
                  onupdate=sa.func.now()),
        schema="itam",
    )
    # (period, cost_center) 유일 — NULL(전체)은 별도 부분 인덱스로 단일 보장
    op.create_index("uq_it_budget_scope", "it_budgets", ["period", "cost_center"],
                    unique=True, schema="itam",
                    postgresql_where=sa.text("cost_center IS NOT NULL"))
    op.create_index("uq_it_budget_total", "it_budgets", ["period"],
                    unique=True, schema="itam",
                    postgresql_where=sa.text("cost_center IS NULL"))


def downgrade() -> None:
    op.drop_table("it_budgets", schema="itam")
