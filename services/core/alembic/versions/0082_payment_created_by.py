"""0082 — 수납 작성자(created_by) 기록: 일마감 직무분리(4-eyes) 보조 통제.

수납 입력자와 마감자가 동일인이면(본인이 그날 수납을 100% 입력) 마감 시 경고/차단.
HIS 인입 수납은 created_by=NULL(시스템)이라 4-eyes 충족으로 간주.

Revision ID: 0082
"""
from alembic import op
import sqlalchemy as sa

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payments",
                  sa.Column("created_by", sa.BigInteger(), nullable=True),
                  schema="patient")


def downgrade() -> None:
    op.drop_column("payments", "created_by", schema="patient")
