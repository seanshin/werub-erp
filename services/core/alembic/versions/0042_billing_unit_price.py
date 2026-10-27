"""0042 — 진료라인 HIS 산정가 동봉: patient.billing_lines.unit_price_his.

제안 B(핸드오프 응답 §1): ① ERP 단가와 대사(검증) ② 비급여(NONCOVERED)는
ERP 마스터 무가격이라 HIS 가격 폴백 — 비급여 16건 공유 전 공백 해소.

Revision ID: 0042
"""
from alembic import op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines "
               "ADD COLUMN unit_price_his NUMERIC(14,2)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN unit_price_his")
