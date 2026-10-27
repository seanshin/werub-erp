"""0025 — 연말 결산분개: 자본(이익잉여금) 계정 도입.

손익(수익·비용)을 이익잉여금으로 대체하는 결산분개의 상대계정. kind=CAPITAL.

Revision ID: 0025
"""
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 이익잉여금(자본) — 없으면 시드. balance_sheet가 CAPITAL을 자본으로 집계.
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind)
        SELECT '3300', '이익잉여금', 'CAPITAL'
        WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '3300')
    """)


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code = '3300'")
