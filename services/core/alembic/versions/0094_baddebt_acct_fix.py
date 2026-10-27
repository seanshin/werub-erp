"""0094 — 대손상각비 계정 5690 시드(0087이 5660 외환차손과 충돌했던 환경 보정).

5660은 기존 외환차손 → 대손상각비는 5690 사용. 이미 0087 적용된 DB(prod)에 5690 안전 시드. 멱등.

Revision ID: 0094
"""
from alembic import op

revision = "0094"
down_revision = "0093"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('5690', '대손상각비', 'EXPENSE', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    pass
