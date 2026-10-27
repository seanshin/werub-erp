"""0033 — 급여 기초정보(보수 프로필): hr.compensation_profiles.

급여 생성 시 매월 수동 입력하던 기본급·부양가족을 직원별 마스터로 영속화.
계좌번호는 컬럼 암호화(AES-GCM, AAD=PK 바인딩 — design §6.4).
HIS 직원 등록 → 동기화 후 ERP 보완입력(온보딩) 대상 식별의 기준 테이블.

Revision ID: 0033
"""
from alembic import op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.compensation_profiles (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL UNIQUE REFERENCES hr.employees(id),
            base_pay NUMERIC(12, 2) NOT NULL DEFAULT 0,
            dependents INTEGER NOT NULL DEFAULT 1,
            bank_name VARCHAR(40),
            bank_account_enc BYTEA,
            effective_from DATE,
            note TEXT,
            updated_by BIGINT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE hr.compensation_profiles")
