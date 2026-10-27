"""0188 — 성과급 전기 시퀀스(취소→재승인 멱등 충돌 방지).

성과급 승인 전표는 source_ref=incentive-{id}로 멱등 전기되는데, 취소(역분개) 후 동일 산정행을
재승인하면 같은 source_ref로 재호출되어 VoucherService 멱등이 **아무 전표도 전기하지 않아**
급여명세(INCENTIVE 라인)만 남고 GL(급여비용/미지급금)은 미전기되는 결함(심층 재검토 HIGH).
posting_seq로 승인 시마다 신규 source_ref(incentive-{id}-{seq}) 부여 → 재승인 시 정상 전기.

Revision ID: 0188
"""
import sqlalchemy as sa
from alembic import op

revision = "0188"
down_revision = "0187"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("incentive_calculations",
                  sa.Column("posting_seq", sa.Integer, nullable=False, server_default="0"),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("incentive_calculations", "posting_seq", schema="hr")
