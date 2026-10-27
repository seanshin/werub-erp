"""0108 — 우발부채→충당부채 전환(K-IFRS 1037) 계정.

기타충당부채(2130)·충당부채전입액(5680). prod 빈코드 확인.

Revision ID: 0108
"""
from alembic import op

revision = "0108"
down_revision = "0107"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("2130", "기타충당부채", "LIABILITY"),
    ("5680", "충당부채전입액", "EXPENSE"),
]


def upgrade() -> None:
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('2130','5680')")
