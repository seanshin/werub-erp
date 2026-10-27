"""0100 — 외화 기말환산(K-IFRS 1021): 외화환산이익·손실 계정.

외화환산이익 4650(미실현, 외환차익 4630과 구분)·외화환산손실 5670.

Revision ID: 0100
"""
from alembic import op

revision = "0100"
down_revision = "0099"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("4650", "외화환산이익", "REVENUE"),
    ("5670", "외화환산손실", "EXPENSE"),
]


def upgrade() -> None:
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('4650','5670')")
