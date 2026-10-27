"""0036 — 퇴직 정산(오프보딩): hr.offboarding_settlements.

퇴사 처리 후 흩어져 있던 정산 조각을 한 절차로: 연차 미사용 수당 + 퇴직금(평균임금)
+ 충당부채 상계 전표. 직원당 1회(UNIQUE) 멱등. 4대보험 상실은 0035에서 자동 적재.

Revision ID: 0036
"""
from alembic import op

revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.offboarding_settlements (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL UNIQUE REFERENCES hr.employees(id),
            resigned_on DATE NOT NULL,
            leave_remaining_days NUMERIC(4,1) NOT NULL DEFAULT 0,
            leave_allowance NUMERIC(14,0) NOT NULL DEFAULT 0,
            avg_monthly_wage NUMERIC(14,0) NOT NULL DEFAULT 0,
            severance_amount NUMERIC(16,0) NOT NULL DEFAULT 0,
            provision_offset NUMERIC(16,0) NOT NULL DEFAULT 0,
            voucher_ref VARCHAR(64),
            settled_by BIGINT,
            settled_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE hr.offboarding_settlements")
