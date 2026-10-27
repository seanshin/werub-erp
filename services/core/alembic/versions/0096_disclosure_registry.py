"""0096 — 공시 등록(K-IFRS 1024·1037·1010): 특수관계자·우발·후속사건.

related_parties·related_party_txns(1024), contingencies(1037), subsequent_events(1010).

Revision ID: 0096
"""
from alembic import op
import sqlalchemy as sa

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "related_parties",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("relation", sa.String(length=40), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )
    op.create_table(
        "related_party_txns",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("party_id", sa.BigInteger(), nullable=False, index=True),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("txn_type", sa.String(length=40), nullable=False),
        sa.Column("amount", sa.Numeric(16, 0), server_default="0"),
        sa.Column("balance", sa.Numeric(16, 0), server_default="0"),
        sa.Column("note", sa.String(length=200), nullable=True),
        schema="finance",
    )
    op.create_table(
        "contingencies",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("kind", sa.String(length=10), server_default="LIABILITY"),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("estimate", sa.Numeric(16, 0), nullable=True),
        sa.Column("likelihood", sa.String(length=10), server_default="중간"),
        sa.Column("status", sa.String(length=10), server_default="OPEN"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )
    op.create_table(
        "subsequent_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("event_date", sa.Date(), nullable=False),
        sa.Column("description", sa.String(length=300), nullable=False),
        sa.Column("adjusting", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reflected", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )


def downgrade() -> None:
    op.drop_table("subsequent_events", schema="finance")
    op.drop_table("contingencies", schema="finance")
    op.drop_table("related_party_txns", schema="finance")
    op.drop_table("related_parties", schema="finance")
