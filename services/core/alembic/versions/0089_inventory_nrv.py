"""0089 — 재고 저가법·원가흐름(K-IFRS 1002): 품목 NRV·cost_method 컬럼 + 평가충당 계정.

scm.items에 nrv_unit(순실현가능가치 단가)·cost_method(FIFO|WAVG) 추가 +
재고자산평가충당금(1159 자산차감)·재고자산평가손실(5540) 계정.

Revision ID: 0089
"""
from alembic import op
import sqlalchemy as sa

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1159", "재고자산평가충당금", "ASSET"),
    ("5540", "재고자산평가손실", "EXPENSE"),
]


def upgrade() -> None:
    op.add_column("items", sa.Column("nrv_unit", sa.Numeric(14, 2), nullable=True), schema="scm")
    op.add_column("items", sa.Column("cost_method", sa.String(length=8), nullable=False,
                                     server_default="FIFO"), schema="scm")
    for code, name, kind in _ACCOUNTS:
        op.execute(
            "INSERT INTO finance.accounts (code, name, kind, active) "
            f"VALUES ('{code}', '{name}', '{kind}', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.drop_column("items", "cost_method", schema="scm")
    op.drop_column("items", "nrv_unit", schema="scm")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1159','5540')")
