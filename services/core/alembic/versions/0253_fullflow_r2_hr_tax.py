"""0253 — 전체 흐름 검토 2차(2026-10-09) C: 퇴직 정산 원천징수 세액 보관 + INCLOCALTAX 카탈로그.

- hr.offboarding_settlements: 퇴직소득세·지방소득세, 연차 미사용 수당 근로소득세·지방소득세(nullable)
  — 정산 전표 2150엔 잡히나 원천징수이행상황(A21)·영수증에 반영할 근거 컬럼이 없었다.
  기존 정산분은 NULL(세액 미보관 — 소급 산정하지 않음).
- hr.pay_item_catalog: INCLOCALTAX(성과급 지방소득세) — 0167이 INCTAX만 시드해 명세서에 원시 코드·
  정렬 100으로 노출됐다.

Revision ID: 0253
Revises: 0252
"""
import sqlalchemy as sa
from alembic import op

revision = "0253"
down_revision = "0252"
branch_labels = None
depends_on = None

_COLS = ("retirement_income_tax", "retirement_local_tax",
         "leave_allowance_tax", "leave_allowance_local_tax")


def upgrade() -> None:
    for c in _COLS:
        op.add_column("offboarding_settlements", sa.Column(c, sa.Numeric(14, 0), nullable=True),
                      schema="hr")
    op.execute("INSERT INTO hr.pay_item_catalog "
               "(code, name, kind, category, taxable, display_order, is_medical) "
               "VALUES ('INCLOCALTAX', '성과급 지방소득세', 'DEDUCTION', '세금', false, 63, true) "
               "ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM hr.pay_item_catalog WHERE code = 'INCLOCALTAX'")
    for c in reversed(_COLS):
        op.drop_column("offboarding_settlements", c, schema="hr")
