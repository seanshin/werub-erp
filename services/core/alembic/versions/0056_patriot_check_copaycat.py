"""0056 — PATRIOT 인라인 CHECK 자동명 제거(0054 누락분) + 차상위 copay_category.

0040 인라인 CHECK가 자동명 billing_lines_coverage_check로 남아 PATRIOT 차단(0054는
ck_bl_coverage만 수정 — 0046 fee_master와 동일 패턴). 자동명 제약 제거.
차상위(NEAR_POOR) 등 copayCategory 수신 — 자격 특수로 HIS copayRate 신뢰.

Revision ID: 0056
"""
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP CONSTRAINT IF EXISTS billing_lines_coverage_check")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN copay_category VARCHAR(20)")  # HIS 본인부담 구분(NEAR_POOR 등)


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN copay_category")
