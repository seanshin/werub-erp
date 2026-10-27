"""0085 — 직원 퇴직 사유(resign_reason): 이직률 분석 사유별 분해.

자발(이직·개인사정·학업)·비자발(계약만료·권고사직)·정년 등.

Revision ID: 0085
"""
from alembic import op
import sqlalchemy as sa

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("employees", sa.Column("resign_reason", sa.String(length=20), nullable=True),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("employees", "resign_reason", schema="hr")
