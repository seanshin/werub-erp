"""0161 — 공단 과다지급 선수금 계정(지급 대사 강화).

공단 지급이 결정액 잔여를 초과하면(선지급·정산복합 지급) 1140 보험미수금이 음수(대변잔액)로
오표시되던 갭 — 초과분을 2141 선수금(공단)으로 분리 전기. 2140은 검진권 전용이라 별도 계정.

Revision ID: 0161
"""
from alembic import op

revision = "0161"
down_revision = "0160"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("2141", "선수금(공단)", "LIABILITY"),
]


def upgrade() -> None:
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code IN ('2141')")
