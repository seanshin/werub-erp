"""0017 — hr.employees 직군·직종·고용형태·SoT(source) + 백필.

의료진/일반직 분리 + 고용형태 + 인사 SoT(D-2): HIS=의료진·정규직 마스터, ERP=비정규 직군.
기존 job_group/external_user_id에서 staff_class·source 백필.

Revision ID: 0017
"""
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.employees ADD COLUMN source VARCHAR(4) NOT NULL DEFAULT 'ERP'")
    op.execute("ALTER TABLE hr.employees ADD COLUMN staff_class VARCHAR(8)")
    op.execute("ALTER TABLE hr.employees ADD COLUMN job_category VARCHAR(24)")
    op.execute("ALTER TABLE hr.employees ADD COLUMN employment_type VARCHAR(12) "
               "NOT NULL DEFAULT 'REGULAR'")
    op.execute("ALTER TABLE hr.employees ADD COLUMN contract_start DATE")
    op.execute("ALTER TABLE hr.employees ADD COLUMN contract_end DATE")
    op.execute("ALTER TABLE hr.employees ADD COLUMN daily_wage NUMERIC(12,2)")
    op.execute("""ALTER TABLE hr.employees ADD CONSTRAINT ck_emp_source
                  CHECK (source IN ('HIS','ERP'))""")
    op.execute("""ALTER TABLE hr.employees ADD CONSTRAINT ck_emp_class
                  CHECK (staff_class IS NULL OR staff_class IN ('MEDICAL','SUPPORT'))""")
    op.execute("""ALTER TABLE hr.employees ADD CONSTRAINT ck_emp_employment
                  CHECK (employment_type IN ('REGULAR','CONTRACT','DAILY','TEMP','WATCH'))""")
    # 백필 — 직군: job_group 매핑
    op.execute("""UPDATE hr.employees SET staff_class =
                    CASE WHEN job_group IN ('DOCTOR','RESIDENT','NURSE','TECH') THEN 'MEDICAL'
                         ELSE 'SUPPORT' END,
                  job_category = job_group""")
    # 백필 — SoT: Clinic/HIS 동기화(external_user_id 보유)는 HIS 관리로
    op.execute("UPDATE hr.employees SET source = 'HIS' WHERE external_user_id IS NOT NULL")
    op.execute("CREATE INDEX ix_emp_class_type ON hr.employees (staff_class, employment_type)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS hr.ix_emp_class_type")
    for c in ("ck_emp_source", "ck_emp_class", "ck_emp_employment"):
        op.execute(f"ALTER TABLE hr.employees DROP CONSTRAINT IF EXISTS {c}")
    for col in ("source", "staff_class", "job_category", "employment_type",
                "contract_start", "contract_end", "daily_wage"):
        op.execute(f"ALTER TABLE hr.employees DROP COLUMN {col}")
