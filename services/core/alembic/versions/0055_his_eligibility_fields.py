"""0055 — HIS 자격 정밀화 payload 수신 필드.

billing.line_recorded에 HIS가 동봉: insuranceProductCode(보험코드·자격검증분)·itemCategory
·eligibilityStatus·copayRate. ERP 단가 우선순위에 insuranceProductCode 반영(MASTER 직행) +
분류·자격 표시.

Revision ID: 0055
"""
from alembic import op

revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN insurance_product_code VARCHAR(20)")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN item_category VARCHAR(20)")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN eligibility_status VARCHAR(16)")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN his_copay_rate NUMERIC(4,3)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN his_copay_rate")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN eligibility_status")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN item_category")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN insurance_product_code")
