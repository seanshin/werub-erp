"""0073 — 검진권 거래 회계 계정(B-G1-B): 선수금·지급수수료.

확정 모델(2026-06-18): 딜러→병원 선지급=선수금(부채)·수수료 거래별 적립.
- 2140 선수금(검진권, LIABILITY): purchase 시 Cr, redeem/refund 시 Dr
- 5520 지급수수료(검진권, EXPENSE): commission 시 Dr(clawback Cr)
정산 외화·원천징수 계정(5660/4630/2150)은 B-G1-C(마이그 후속)에서 추가.

Revision ID: 0073
"""
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('2140','선수금(검진권)','LIABILITY'),
            ('5520','지급수수료(검진권)','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    # 전표에서 참조 중일 수 있어 보존(계정 삭제는 집계 정합 위험)
    pass
