"""0104 — 출고원가 COGS(K-IFRS 1002): stock_movements.cost.

출고(OUT/DISPOSE) 시 cost_method(FIFO=실제 lot 단가 / WAVG=이동평균)로 원가를 산정·기록.

Revision ID: 0104
"""
from alembic import op
import sqlalchemy as sa

revision = "0104"
down_revision = "0103"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("stock_movements", sa.Column("cost", sa.Numeric(16, 2), nullable=True),
                  schema="scm")


def downgrade() -> None:
    op.drop_column("stock_movements", "cost", schema="scm")
