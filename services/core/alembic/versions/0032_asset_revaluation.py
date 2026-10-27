"""0032 — 고정자산 재평가(K-IFRS): 재평가누계 컬럼 + 재평가잉여금(3400·자본)·손실(5650).

공정가치 > 장부가액 → 자산↑/재평가잉여금(OCI·자본). 하락은 기존 잉여금 차감 후 초과분 손실.

Revision ID: 0032
"""
from alembic import op

revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE finance.fixed_assets "
               "ADD COLUMN accumulated_revaluation NUMERIC(16,0) NOT NULL DEFAULT 0")
    for code, name, kind in [("3400", "재평가잉여금", "CAPITAL"),
                             ("5650", "재평가손실", "EXPENSE")]:
        op.execute(f"""
            INSERT INTO finance.accounts (code, name, kind)
            SELECT '{code}', '{name}', '{kind}'
            WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '{code}')
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE finance.fixed_assets DROP COLUMN accumulated_revaluation")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('3400','5650')")
