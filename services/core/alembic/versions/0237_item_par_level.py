"""0237 — 품목 par_level(보충 목표·자동발주·P3-7).

안전재고(재주문점) 미만 시 par_level(order-up-to)까지 보충 제안. scm.items.par_level 추가.

Revision ID: 0237
"""
import sqlalchemy as sa
from alembic import op

revision = "0237"
down_revision = "0236"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("par_level", sa.Numeric(12, 2), nullable=True),
                  schema="scm")


def downgrade() -> None:
    op.drop_column("items", "par_level", schema="scm")
