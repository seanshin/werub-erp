"""0080 — 사전심사 의료진 확인 게이트(의료법: 비의료인 의학판단 금지).

의학판단 룰(physician_gated)은 심사자 단독 오탐 불가 → 의료진 질의·확인 경유로만 종결.
- pre_review_rules.physician_gated: 의학판단 필요 룰 표시(관리자 설정).
- pre_review_queries.confirmed_by_employee: 종결 시 확인 책임 의료진(employee_id).
기본 게이트: 임상적정성 룰타입(EXCLUSIVE_PAIR·AGE_SEX_LIMIT·PERIOD_LIMIT·LOS_RATIO) ON.

Revision ID: 0080
"""
from alembic import op
import sqlalchemy as sa

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pre_review_rules",
                  sa.Column("physician_gated", sa.Boolean(), nullable=False,
                            server_default=sa.false()),
                  schema="claims")
    op.add_column("pre_review_queries",
                  sa.Column("confirmed_by_employee", sa.BigInteger(), nullable=True),
                  schema="claims")
    # 임상적정성 판단 룰 기본 게이트 ON(관리자가 /pre-review/rules에서 조정 가능)
    op.execute("""
        UPDATE claims.pre_review_rules SET physician_gated = true
        WHERE rule_type IN ('EXCLUSIVE_PAIR','AGE_SEX_LIMIT','PERIOD_LIMIT','LOS_RATIO')
    """)


def downgrade() -> None:
    op.drop_column("pre_review_queries", "confirmed_by_employee", schema="claims")
    op.drop_column("pre_review_rules", "physician_gated", schema="claims")
