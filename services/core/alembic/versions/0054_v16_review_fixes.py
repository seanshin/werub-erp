"""0054 — v1.6 점검 수정: 보훈(PATRIOT) coverage CHECK 확장.

C1: _COVERAGE_MAP이 PATRIOT를 저장하나 billing_lines CHECK 미허용 → 보훈 환자 라인 인입 실패.
엔진 split_line PATRIOT 분기가 의도이므로 CHECK 확장.

Revision ID: 0054
"""
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP CONSTRAINT IF EXISTS ck_bl_coverage")
    op.execute("ALTER TABLE patient.billing_lines ADD CONSTRAINT ck_bl_coverage "
               "CHECK (coverage IN ('NHIS','NONCOVERED','AUTO','INDUSTRIAL','MEDICAL_AID','PATRIOT'))")


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP CONSTRAINT IF EXISTS ck_bl_coverage")
    op.execute("ALTER TABLE patient.billing_lines ADD CONSTRAINT ck_bl_coverage "
               "CHECK (coverage IN ('NHIS','NONCOVERED','AUTO','INDUSTRIAL','MEDICAL_AID'))")
