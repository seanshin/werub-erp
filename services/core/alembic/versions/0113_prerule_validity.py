"""0113 — 사전심사 룰 고시 버저닝(P1 G7): pre_review_rules.valid_from/valid_to.

고시 효력일 기반 시점 유효룰 — 진료(기준)일에 유효한 룰만 평가. 효력일 자동적용·
구 룰 폐기(valid_to). 재-sync 시 시점 기준 재평가(소급). NULL=상시 유효.

Revision ID: 0113
"""
import sqlalchemy as sa
from alembic import op

revision = "0113"
down_revision = "0112"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pre_review_rules", sa.Column("valid_from", sa.Date(), nullable=True),
                  schema="claims")
    op.add_column("pre_review_rules", sa.Column("valid_to", sa.Date(), nullable=True),
                  schema="claims")


def downgrade() -> None:
    op.drop_column("pre_review_rules", "valid_to", schema="claims")
    op.drop_column("pre_review_rules", "valid_from", schema="claims")
