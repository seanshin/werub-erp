"""0053 — 약품 코드 매핑(HIS 비표준코드 ↔ 보험 제품코드) 상시 레이어.

HIS가 비표준 약품코드(M*)로 청구 → 명칭/성분 매칭으로 보험코드 후보 → 약제팀 확정
→ 이후 자동 변환(보험코드→fee_master 단가). billing_lines에 drug_name·ingredient 동봉 수신.

Revision ID: 0053
"""
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # HIS 약품 명칭·성분 동봉 수신(매칭 근거)
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN drug_name VARCHAR(200)")
    op.execute("ALTER TABLE patient.billing_lines ADD COLUMN drug_ingredient VARCHAR(200)")
    op.execute("""
        CREATE TABLE regulatory.drug_code_map (
            id BIGSERIAL PRIMARY KEY,
            his_code VARCHAR(20) NOT NULL UNIQUE,
            insurance_code VARCHAR(20),                    -- 확정 보험 제품코드
            drug_name VARCHAR(200),
            ingredient VARCHAR(200),
            status VARCHAR(10) NOT NULL DEFAULT 'SUGGESTED'
                CHECK (status IN ('SUGGESTED','CONFIRMED','REJECTED')),
            confidence VARCHAR(8),                         -- HIGH|MEDIUM|LOW
            candidates JSONB,                              -- 자동 매칭 후보 목록
            matched_name VARCHAR(200),
            confirmed_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_dcm_status ON regulatory.drug_code_map(status)")


def downgrade() -> None:
    op.execute("DROP TABLE regulatory.drug_code_map")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN drug_ingredient")
    op.execute("ALTER TABLE patient.billing_lines DROP COLUMN drug_name")
