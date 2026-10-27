"""0083 — 계정과목 active(사용/비활성) 플래그: COA 관리(소프트 삭제·신규입력 후보 제어).

전표 라인은 account_code 문자열 참조(FK 아님) → 계정 하드삭제는 과거 집계 탈락 위험.
비활성(active=false)으로 신규 사용만 막고 과거 전표·재무제표 집계는 그대로 유지.

Revision ID: 0083
"""
from alembic import op
import sqlalchemy as sa

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts",
                  sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
                  schema="finance")


def downgrade() -> None:
    op.drop_column("accounts", "active", schema="finance")
