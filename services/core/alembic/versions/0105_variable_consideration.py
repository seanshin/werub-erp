"""0105 — 변동대가(K-IFRS 1115): 환불부채 계정(2155).

미결정 청구의 예상 삭감(변동대가)을 수익에서 차감하고 환불부채로 인식. 결정 시 정산.

Revision ID: 0105
"""
from alembic import op

revision = "0105"
down_revision = "0104"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('2155', '환불부채(변동대가)', 'LIABILITY', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code = '2155'")
