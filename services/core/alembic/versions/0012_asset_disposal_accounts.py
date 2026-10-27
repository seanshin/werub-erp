"""0012 — 유형자산 처분손익 계정 (처분 전표 — 자산제거·누계상계·처분손익).

Revision ID: 0012
"""
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('4610','유형자산처분이익','REVENUE'),
            ('5610','유형자산처분손실','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('4610','5610')")
