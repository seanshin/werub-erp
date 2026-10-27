"""0007 — hr.employees.external_user_id (Clinic 직원 동기화 키 + 근태 매핑 토대, FR-28).

Revision ID: 0007
"""
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.employees ADD COLUMN external_user_id VARCHAR(64)")
    # 외부(Clinic) 직원 식별자 — 동기화 멱등키 + 근태 userId 매핑. NULL=내부 등록 직원.
    op.execute("CREATE UNIQUE INDEX uq_employee_external_user "
               "ON hr.employees (external_user_id) WHERE external_user_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS hr.uq_employee_external_user")
    op.execute("ALTER TABLE hr.employees DROP COLUMN external_user_id")
