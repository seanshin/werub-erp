"""0095 — 리스 정밀화(K-IFRS 1116): 단기·소액 면제 컬럼 + 단기리스료 계정.

lease_contracts.exempt(단기·소액 면제 — 사용권자산 미인식, 비용 직접) + 단기·소액리스료(5645).

Revision ID: 0095
"""
from alembic import op
import sqlalchemy as sa

revision = "0095"
down_revision = "0094"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("lease_contracts",
                  sa.Column("exempt", sa.Boolean(), nullable=False, server_default=sa.false()),
                  schema="finance")
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('5645', '단기·소액리스료', 'EXPENSE', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.drop_column("lease_contracts", "exempt", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code = '5645'")
