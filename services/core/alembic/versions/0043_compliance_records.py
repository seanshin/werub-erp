"""0043 — 법정의무교육·직원 건강검진 추적: hr.compliance_records (인사 P3).

의료기관 인증평가·노동법 의무: 법정교육 4종(성희롱예방·개인정보·산업안전·장애인식),
건강검진(일반·특수=방사선 종사자). 이수 기록 → 주기별 만료 산정 → 만료/임박 가시화.

Revision ID: 0043
"""
from alembic import op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.compliance_records (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            kind VARCHAR(8) NOT NULL CHECK (kind IN ('TRAINING','CHECKUP')),
            category VARCHAR(24) NOT NULL,
            done_on DATE NOT NULL,
            valid_until DATE NOT NULL,
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_compliance UNIQUE (employee_id, category, done_on)
        )
    """)
    op.execute("CREATE INDEX ix_compliance_emp ON hr.compliance_records (employee_id)")
    op.execute("CREATE INDEX ix_compliance_until ON hr.compliance_records (valid_until)")


def downgrade() -> None:
    op.execute("DROP TABLE hr.compliance_records")
