"""0029 — 고정자산 손상차손: 손상누계 컬럼 + 계정(5620·1815).

회수가능액 < 장부가액(취득−감가누계−손상누계) 시 손상차손 인식. 장부가액 차감.

Revision ID: 0029
"""
from alembic import op

revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE finance.fixed_assets "
               "ADD COLUMN accumulated_impairment NUMERIC(16,0) NOT NULL DEFAULT 0")
    for code, name, kind in [("5620", "유형자산손상차손", "EXPENSE"),
                             ("1815", "손상차손누계액", "ASSET")]:
        op.execute(f"""
            INSERT INTO finance.accounts (code, name, kind)
            SELECT '{code}', '{name}', '{kind}'
            WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '{code}')
        """)


def downgrade() -> None:
    op.execute("ALTER TABLE finance.fixed_assets DROP COLUMN accumulated_impairment")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('5620','1815')")
