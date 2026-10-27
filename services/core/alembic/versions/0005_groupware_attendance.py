"""0005 — 그룹웨어(Clinic) 근태 수신 스테이징 (integration 스키마, M0a=REST, FR-28/29).

Revision ID: 0005
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE integration.groupware_attendance_staging (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_ref VARCHAR(80) NOT NULL UNIQUE,
      external_user_id VARCHAR(64) NOT NULL,
      employee_no VARCHAR(20),
      name VARCHAR(80),
      department VARCHAR(80),
      clock_in TIMESTAMPTZ,
      clock_out TIMESTAMPTZ,
      work_on DATE NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW','APPLIED','FAILED')),
      error_detail JSONB,
      received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    # 소비 잡 조회 경로: status='NEW' 스캔, 사번×월 재집계
    op.execute("CREATE INDEX ix_gw_att_status ON integration.groupware_attendance_staging (status)")
    op.execute("CREATE INDEX ix_gw_att_emp ON "
               "integration.groupware_attendance_staging (employee_no, work_on)")


def downgrade() -> None:
    op.execute("DROP TABLE integration.groupware_attendance_staging")
