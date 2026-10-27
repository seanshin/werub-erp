"""0097 — 공정가치 서열(K-IFRS 1113): fixed_assets.fair_value_level.

재평가 자산의 공정가치 측정 수준(Level 1/2/3) 기록 → 공정가치 서열 공시.

Revision ID: 0097
"""
from alembic import op
import sqlalchemy as sa

revision = "0097"
down_revision = "0096"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fixed_assets", sa.Column("fair_value_level", sa.String(length=1), nullable=True),
                  schema="finance")


def downgrade() -> None:
    op.drop_column("fixed_assets", "fair_value_level", schema="finance")
