"""0107 — 무형자산 손상(1036)·환입·처분(1038): 컬럼 + 계정.

intangible_assets에 accumulated_impairment·disposed_on 추가.
계정(신규, prod 빈코드 확인): 무형자산손상차손누계(1856)·무형자산손상차손(5216)·
무형자산손상차손환입(4641)·무형자산처분이익(4615)·무형자산처분손실(5616).

Revision ID: 0107
"""
import sqlalchemy as sa
from alembic import op

revision = "0107"
down_revision = "0106"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1856", "무형자산손상차손누계", "ASSET"),
    ("5216", "무형자산손상차손", "EXPENSE"),
    ("4641", "무형자산손상차손환입", "REVENUE"),
    ("4615", "무형자산처분이익", "REVENUE"),
    ("5616", "무형자산처분손실", "EXPENSE"),
]


def upgrade() -> None:
    op.add_column("intangible_assets",
                  sa.Column("accumulated_impairment", sa.Numeric(16, 0), server_default="0"),
                  schema="finance")
    op.add_column("intangible_assets",
                  sa.Column("disposed_on", sa.Date(), nullable=True),
                  schema="finance")
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN "
               "('1856','5216','4641','4615','5616')")
    op.drop_column("intangible_assets", "disposed_on", schema="finance")
    op.drop_column("intangible_assets", "accumulated_impairment", schema="finance")
