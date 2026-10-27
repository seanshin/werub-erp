"""0091 — 재무제표 주석(K-IFRS 1001): finance.disclosure_notes.

정량주석(유형자산 증감·충당부채 증감 등)은 자동 산출, 정성주석(회계정책 등)은 수기 편집. (period,note_key) 고유.

Revision ID: 0091
"""
from alembic import op
import sqlalchemy as sa

revision = "0091"
down_revision = "0090"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "disclosure_notes",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("note_key", sa.String(length=40), nullable=False),
        sa.Column("title", sa.String(length=120), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("period", "note_key", name="uq_disclosure_note"),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("disclosure_notes", schema="finance")
