"""0099 — 회계정책·추정변경·오류(K-IFRS 1008): finance.accounting_changes.

변경유형(POLICY/ESTIMATE/ERROR)·처리방법(소급/전진)·영향액 등록 → 공시.

Revision ID: 0099
"""
from alembic import op
import sqlalchemy as sa

revision = "0099"
down_revision = "0098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_changes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("change_type", sa.String(length=10), nullable=False),
        sa.Column("standard", sa.String(length=40), nullable=True),
        sa.Column("description", sa.String(length=400), nullable=False),
        sa.Column("treatment", sa.String(length=14), server_default="PROSPECTIVE"),
        sa.Column("effect_amount", sa.Numeric(16, 0), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("accounting_changes", schema="finance")
