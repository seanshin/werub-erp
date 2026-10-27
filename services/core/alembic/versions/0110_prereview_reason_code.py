"""0110 — 사전심사 거부사유 코드화(P0-2): pre_review_findings.reason_group/reason_code.

적발에 거부사유 대분류(A/B/D/K/M)·세분코드(예: B-CNT) 부여 — 설명가능성·통계·학습 피처.
룰타입 기준 스탬프(pre_review.REASON_BY_TYPE). 기존 행은 NULL(재실행 시 채워짐).

Revision ID: 0110
"""
import sqlalchemy as sa
from alembic import op

revision = "0110"
down_revision = "0109"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pre_review_findings",
                  sa.Column("reason_group", sa.String(length=2), nullable=True), schema="claims")
    op.add_column("pre_review_findings",
                  sa.Column("reason_code", sa.String(length=12), nullable=True), schema="claims")


def downgrade() -> None:
    op.drop_column("pre_review_findings", "reason_code", schema="claims")
    op.drop_column("pre_review_findings", "reason_group", schema="claims")
