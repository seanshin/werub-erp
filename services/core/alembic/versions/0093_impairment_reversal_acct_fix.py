"""0093 — 손상차손환입 계정 4640 시드(0092가 4630 외환차익과 충돌했던 환경 보정).

이미 0092 적용된 DB(4630 충돌로 4640 미생성)에서 4640을 안전 시드. 멱등.

Revision ID: 0093
"""
from alembic import op

revision = "0093"
down_revision = "0092"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('4640', '유형자산손상차손환입', 'REVENUE', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    pass
