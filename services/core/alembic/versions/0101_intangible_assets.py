"""0101 — 무형자산(K-IFRS 1038)·차입원가(1023): intangible_assets + 계정.

무형자산(1850)·무형자산상각누계(1855)·무형자산상각비(5215). 정액 상각·차입원가 자본화.

Revision ID: 0101
"""
from alembic import op
import sqlalchemy as sa

revision = "0101"
down_revision = "0100"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1850", "무형자산", "ASSET"),
    ("1855", "무형자산상각누계", "ASSET"),
    ("5215", "무형자산상각비", "EXPENSE"),
]


def upgrade() -> None:
    op.create_table(
        "intangible_assets",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("asset_no", sa.String(length=30), nullable=False, unique=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("acquired_on", sa.Date(), nullable=False),
        sa.Column("cost", sa.Numeric(16, 0), nullable=False),
        sa.Column("useful_life_years", sa.Integer(), nullable=False),
        sa.Column("accumulated_amort", sa.Numeric(16, 0), server_default="0"),
        sa.Column("capitalized_borrowing", sa.Numeric(16, 0), server_default="0"),
        sa.Column("status", sa.String(length=10), server_default="IN_USE"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('IN_USE','DISPOSED')", name="ck_intangible_status"),
        schema="finance",
    )
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.drop_table("intangible_assets", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1850','1855','5215')")
