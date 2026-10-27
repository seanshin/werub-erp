"""0011 — 재고자산 계정(1150) — 입고검수 시 재고자산/미지급 전표(procure-to-pay).

Revision ID: 0011
"""
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('1150','저장품(재고자산)','ASSET'),
            ('5410','재료비','EXPENSE')
        ON CONFLICT (code) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1150','5410')")
