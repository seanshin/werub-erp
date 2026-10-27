"""0002 — hr 스키마 테이블 (design §3.5, M3).

Revision ID: 0002
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE hr.employees (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_no VARCHAR(20) NOT NULL UNIQUE,
      name VARCHAR(80) NOT NULL,
      birth_date DATE,
      phone_enc BYTEA,
      address_enc BYTEA,
      department_id BIGINT,
      position VARCHAR(40),
      job_group VARCHAR(16) NOT NULL DEFAULT 'STAFF',
      hired_on DATE NOT NULL,
      resigned_on DATE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE hr.appointments (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      appointment_type VARCHAR(16) NOT NULL,
      from_department_id BIGINT, to_department_id BIGINT,
      from_position VARCHAR(40), to_position VARCHAR(40),
      effective_on DATE NOT NULL,
      note TEXT
    )""")
    op.execute("CREATE INDEX ix_appt_emp ON hr.appointments (employee_id)")
    op.execute("""
    CREATE TABLE hr.licenses (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      license_type VARCHAR(40) NOT NULL,
      license_no VARCHAR(40) NOT NULL,
      issued_on DATE, expires_on DATE,
      training_completed_on DATE
    )""")
    op.execute("CREATE INDEX ix_license_expiry ON hr.licenses (expires_on)")
    op.execute("""
    CREATE TABLE hr.leave_grants (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      grant_year INT NOT NULL,
      granted_days NUMERIC(4,1) NOT NULL,
      basis VARCHAR(200) NOT NULL,
      CONSTRAINT uq_leave_grant_year UNIQUE (employee_id, grant_year)
    )""")
    op.execute("""
    CREATE TABLE hr.leave_requests (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      leave_type VARCHAR(16) NOT NULL DEFAULT 'ANNUAL',
      start_on DATE NOT NULL, end_on DATE NOT NULL,
      days NUMERIC(4,1) NOT NULL,
      status VARCHAR(12) NOT NULL DEFAULT 'REQUESTED'
        CHECK (status IN ('REQUESTED','APPROVED','REJECTED','CANCELED')),
      reason TEXT,
      decided_by BIGINT, decided_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE hr.duty_schedules (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      department_id BIGINT NOT NULL,
      yyyymm VARCHAR(7) NOT NULL,
      status VARCHAR(12) NOT NULL DEFAULT 'DRAFT',
      published_at TIMESTAMPTZ,
      CONSTRAINT uq_duty_month UNIQUE (department_id, yyyymm)
    )""")
    op.execute("""
    CREATE TABLE hr.duty_assignments (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      schedule_id BIGINT NOT NULL REFERENCES hr.duty_schedules(id),
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      duty_on DATE NOT NULL,
      shift VARCHAR(3) NOT NULL CHECK (shift IN ('D','E','N','OFF')),
      CONSTRAINT uq_duty_cell UNIQUE (schedule_id, employee_id, duty_on)
    )""")
    op.execute("""
    CREATE TABLE hr.payroll_records (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      yyyymm VARCHAR(7) NOT NULL,
      confirmed_at TIMESTAMPTZ, confirmed_by BIGINT,
      CONSTRAINT uq_payroll_month UNIQUE (employee_id, yyyymm)
    )""")
    op.execute("""
    CREATE TABLE hr.payroll_lines (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      record_id BIGINT NOT NULL REFERENCES hr.payroll_records(id),
      item_code VARCHAR(20) NOT NULL,
      item_kind VARCHAR(10) NOT NULL CHECK (item_kind IN ('EARNING','DEDUCTION')),
      amount NUMERIC(12,0) NOT NULL
    )""")
    op.execute("""
    CREATE TABLE hr.attendance_summaries (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      yyyymm VARCHAR(7) NOT NULL,
      work_days INT NOT NULL DEFAULT 0,
      late_count INT NOT NULL DEFAULT 0,
      early_leave_count INT NOT NULL DEFAULT 0,
      night_shift_count INT NOT NULL DEFAULT 0,
      total_work_minutes INT NOT NULL DEFAULT 0,
      CONSTRAINT uq_attendance_month UNIQUE (employee_id, yyyymm)
    )""")
    op.execute("""
    CREATE TABLE hr.certificate_issues (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
      cert_type VARCHAR(12) NOT NULL CHECK (cert_type IN ('EMPLOYMENT','CAREER')),
      issued_to BIGINT NOT NULL,
      purpose VARCHAR(200),
      issued_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA hr TO erp_app;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA hr TO erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    for t in ["certificate_issues", "attendance_summaries", "payroll_lines", "payroll_records",
              "duty_assignments", "duty_schedules", "leave_requests", "leave_grants",
              "licenses", "appointments", "employees"]:
        op.execute(f"DROP TABLE IF EXISTS hr.{t}")
