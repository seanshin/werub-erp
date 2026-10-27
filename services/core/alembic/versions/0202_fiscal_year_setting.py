"""0202 — 회계연도 시작월 설정: common.org_settings.fiscal_year_start_month.

FY N = N년 시작월 1일 ~ +12개월−1일. 기본 1(역년 — 현행 무회귀). 재무제표(연간)·연말결산·공시·
이연법인세에 적용. 세무(부가세 과세기간)·연말정산·4대보험은 법정 달력연도 유지(fiscal.py 문서화).

Revision ID: 0202
"""
import sqlalchemy as sa
from alembic import op

revision = "0202"
down_revision = "0201"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_settings",
                  sa.Column("fiscal_year_start_month", sa.Integer(), nullable=False,
                            server_default="1"),
                  schema="common")
    op.create_check_constraint("ck_org_fiscal_start_month", "org_settings",
                               "fiscal_year_start_month BETWEEN 1 AND 12", schema="common")


def downgrade() -> None:
    op.drop_constraint("ck_org_fiscal_start_month", "org_settings", schema="common")
    op.drop_column("org_settings", "fiscal_year_start_month", schema="common")
