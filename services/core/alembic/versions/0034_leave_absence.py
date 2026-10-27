"""0034 — 휴직/복직: hr.leave_absences.

육아휴직·출산휴가·병가·산재·일반휴직 기간 관리. 휴직 중 급여 일괄생성 제외,
4대보험 납부예외 식별(insurance_exempt). 발령 이력(LEAVE/REINSTATE)과 연동.
근속(퇴직금 추계)은 법령상 휴직 포함 — 충당 적립은 변경 없음.

Revision ID: 0034
"""
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.leave_absences (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            kind VARCHAR(12) NOT NULL
                CHECK (kind IN ('MATERNITY','PARENTAL','SICK','INDUSTRIAL','PERSONAL')),
            start_on DATE NOT NULL,
            expected_return_on DATE,
            return_on DATE,
            insurance_exempt BOOLEAN NOT NULL DEFAULT true,
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_leave_absences_employee ON hr.leave_absences (employee_id)")


def downgrade() -> None:
    op.execute("DROP TABLE hr.leave_absences")
