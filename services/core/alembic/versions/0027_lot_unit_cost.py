"""0027 — scm.lots 단가(unit_cost): 재고자산 평가(FIFO 원가) 기반.

입고 로트에 매입단가 보존 → 잔여수량×단가 = 재고자산 평가액.

Revision ID: 0027
"""
from alembic import op

revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scm.lots ADD COLUMN unit_cost NUMERIC(14,2)")


def downgrade() -> None:
    op.execute("ALTER TABLE scm.lots DROP COLUMN unit_cost")
