"""0048 — hr.employees.email: SSO JIT ↔ 직원 연결 키.

staff 이벤트의 email을 직원에 저장 → 직원이 '나중에' 첫 SSO 로그인(JIT 생성)해도
그 시점에 email로 employee_id 연결(이벤트 시점 링크의 시간 공백 해소).

Revision ID: 0048
"""
from alembic import op

revision = "0048"
down_revision = "0047"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.employees ADD COLUMN email VARCHAR(255)")
    op.execute("CREATE INDEX ix_employees_email ON hr.employees (lower(email))")


def downgrade() -> None:
    op.execute("DROP INDEX hr.ix_employees_email")
    op.execute("ALTER TABLE hr.employees DROP COLUMN email")
