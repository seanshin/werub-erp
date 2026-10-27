"""0050 — 기관 종별 본인부담률 설정(시점관리) + BillingLine 특례/급여종 필드.

기관 종별(병원 기본 40%)을 런타임 변경·시점관리. 종별 변경 시 신규 행(effective_from)
→ 과거 진료는 당시 종별로 계산(소급 오류 방지). 산정특례·의료급여 종별은 HIS 동봉 대기
필드만 선반영(폴백 안전).

Revision ID: 0050
"""
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE regulatory.institution_grade_settings (
            id BIGSERIAL PRIMARY KEY,
            grade VARCHAR(12) NOT NULL CHECK (grade IN ('CLINIC','HOSPITAL','GENERAL','TERTIARY')),
            outpatient_rate NUMERIC(4,3) NOT NULL,
            inpatient_rate NUMERIC(4,3) NOT NULL DEFAULT 0.20,
            meal_rate NUMERIC(4,3) NOT NULL DEFAULT 0.50,
            special_case_rate NUMERIC(4,3) NOT NULL DEFAULT 0.05,
            effective_from DATE NOT NULL,
            note VARCHAR(200),
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_inst_grade_from UNIQUE (effective_from)
        )
    """)
    # 시드 — 위브병원: 병원 종별 외래 40%
    op.execute("""
        INSERT INTO regulatory.institution_grade_settings
            (grade, outpatient_rate, effective_from, note)
        VALUES ('HOSPITAL', 0.40, '2026-01-01', '초기 설정 — 병원 종별')
    """)
    # 산정특례·의료급여 종별 — HIS 동봉 대기(폴백 안전: NULL=일반율)
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN special_case_type VARCHAR(20)")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN medical_aid_class VARCHAR(4)")


def downgrade() -> None:
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN medical_aid_class")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN special_case_type")
    op.execute("DROP TABLE regulatory.institution_grade_settings")
