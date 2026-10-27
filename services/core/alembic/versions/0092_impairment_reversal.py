"""0092 — 자산손상 환입(K-IFRS 1036): 손상차손환입 계정(4640).

손상 사유 소멸 시 원가모형 한도 내 환입을 당기손익(수익)으로 인식.
(주의: 4630은 기존 외환차익 — 손상환입은 4640 사용.)

Revision ID: 0092
"""
from alembic import op

revision = "0092"
down_revision = "0091"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('4640', '유형자산손상차손환입', 'REVENUE', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code = '4640'")
