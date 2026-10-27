"""0102 — 기능별 비용 구분(K-IFRS 1001): accounts.cost_function + 기본 분류 시드.

COGS(매출원가/의료원가)·SGA(판매관리비)·OTHER(영업외/기타). 기능별 손익계산서(매출총이익·영업이익).
기본 시드는 관례적 분류 — 관리자가 COA에서 조정.

Revision ID: 0102
"""
from alembic import op
import sqlalchemy as sa

revision = "0102"
down_revision = "0101"
branch_labels = None
depends_on = None

# 관례적 기본 분류(의료기관) — 의료원가 vs 판관비 vs 영업외
_COGS = ("5110", "5210", "5310", "5320", "5410", "5630", "5640")   # 진료·감가·급여·퇴직·재료·리스
_SGA = ("5215", "5520", "5530", "5540", "5690")                    # 무형상각·수수료·감모·재고평가·대손
_OTHER = ("5610", "5620", "5650", "5660", "5670")                  # 처분·손상·재평가손실·외환차손·환산손실


def upgrade() -> None:
    op.add_column("accounts", sa.Column("cost_function", sa.String(length=6), nullable=True),
                  schema="finance")
    for fn, codes in (("COGS", _COGS), ("SGA", _SGA), ("OTHER", _OTHER)):
        lst = ",".join(f"'{c}'" for c in codes)
        op.execute(f"UPDATE finance.accounts SET cost_function = '{fn}' "
                   f"WHERE code IN ({lst}) AND cost_function IS NULL")


def downgrade() -> None:
    op.drop_column("accounts", "cost_function", schema="finance")
