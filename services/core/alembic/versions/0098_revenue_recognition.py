"""0098 — 수익인식(K-IFRS 1115): 계약자산(미청구수익)·계약부채 계정.

계약자산 1135(미청구수익)·계약부채 2145. 기간말 미청구수익 발생(발생주의)·차기 환입.

Revision ID: 0098
"""
from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1135", "계약자산(미청구수익)", "ASSET"),
    ("2145", "계약부채", "LIABILITY"),
]


def upgrade() -> None:
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1135','2145')")
