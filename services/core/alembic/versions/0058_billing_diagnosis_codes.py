"""0058 — 진료라인에 상병코드(진단) 추가.

의학적 타당성 심사를 위해 EDI 청구명세서의 상병코드(진단)를 billing_lines에 수신.
HIS billing.line_recorded payload의 diagnosisCodes(쉼표구분)를 저장. 미수신 시 NULL.

Revision ID: 0058
"""
from alembic import op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN diagnosis_codes VARCHAR(120)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN diagnosis_codes")
