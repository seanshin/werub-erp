"""0040 — 진료 라인 미러: patient.billing_lines (D-6 진료비 SoT).

HIS billing.line_recorded 수신분 — ERP 진료비 책정·사전심사의 원천 사실.
line_id 멱등(치환), cancelled=취소 라인(집계 제외). 스키마는 요청서
his-billing-lines.request.md 제안 기준(HIS 회신 시 consumer만 보정).

Revision ID: 0040
"""
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE patient.billing_lines (
            id BIGSERIAL PRIMARY KEY,
            line_id VARCHAR(64) NOT NULL UNIQUE,
            encounter_id VARCHAR(64),
            chart_no VARCHAR(20) NOT NULL,
            encounter_type VARCHAR(12) NOT NULL DEFAULT 'OUTPATIENT'
                CHECK (encounter_type IN ('OUTPATIENT','INPATIENT')),
            admitted_on DATE,
            dept_code VARCHAR(10),
            code VARCHAR(20) NOT NULL,
            code_type VARCHAR(16) NOT NULL DEFAULT 'TARIFF',
            count NUMERIC(8,2) NOT NULL DEFAULT 1,
            treated_on DATE,
            treated_at TIMESTAMPTZ,
            patient_age INTEGER,
            patient_sex VARCHAR(1),
            coverage VARCHAR(12) NOT NULL DEFAULT 'NHIS'
                CHECK (coverage IN ('NHIS','NONCOVERED','AUTO','INDUSTRIAL','MEDICAL_AID')),
            cancelled BOOLEAN NOT NULL DEFAULT false,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_billing_lines_chart ON patient.billing_lines (chart_no)")
    op.execute("CREATE INDEX ix_billing_lines_encounter ON patient.billing_lines (encounter_id)")


def downgrade() -> None:
    op.execute("DROP TABLE patient.billing_lines")
