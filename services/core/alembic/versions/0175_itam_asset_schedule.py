"""0175 — ITAM: IT 자산 예정일(정기점검·정비·회수).

it_assets에 next_inspection_on(정기점검)·next_maintenance_on(정비)·return_due_on(회수) 추가.

Revision ID: 0175
"""
import sqlalchemy as sa
from alembic import op

revision = "0175"
down_revision = "0174"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("it_assets", sa.Column("next_inspection_on", sa.Date), schema="itam")
    op.add_column("it_assets", sa.Column("next_maintenance_on", sa.Date), schema="itam")
    op.add_column("it_assets", sa.Column("return_due_on", sa.Date), schema="itam")


def downgrade() -> None:
    op.drop_column("it_assets", "return_due_on", schema="itam")
    op.drop_column("it_assets", "next_maintenance_on", schema="itam")
    op.drop_column("it_assets", "next_inspection_on", schema="itam")
