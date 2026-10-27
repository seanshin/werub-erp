"""0006 — HIS 환자·수납 수신 스테이징 (integration 스키마, M0a=REST, FR-15/16).

Revision ID: 0006
"""
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE integration.his_patient_staging (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_ref VARCHAR(80) NOT NULL UNIQUE,
      chart_no VARCHAR(20) NOT NULL,
      name VARCHAR(80) NOT NULL,
      birth_date DATE NOT NULL,
      phone VARCHAR(20),
      source_seen_at TIMESTAMPTZ NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW','APPLIED','FAILED')),
      error_detail JSONB,
      received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_his_patient_status ON integration.his_patient_staging (status)")
    op.execute("CREATE INDEX ix_his_patient_chart ON integration.his_patient_staging (chart_no)")

    op.execute("""
    CREATE TABLE integration.his_payment_staging (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_ref VARCHAR(80) NOT NULL UNIQUE,
      chart_no VARCHAR(20) NOT NULL,
      amount NUMERIC(14,0) NOT NULL,
      method VARCHAR(10) NOT NULL DEFAULT 'CARD',
      paid_on DATE NOT NULL,
      dept_code VARCHAR(10),
      doctor_id BIGINT,
      status VARCHAR(10) NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW','APPLIED','FAILED')),
      error_detail JSONB,
      received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_his_payment_status ON integration.his_payment_staging (status)")
    op.execute("CREATE INDEX ix_his_payment_chart ON integration.his_payment_staging (chart_no, paid_on)")


def downgrade() -> None:
    op.execute("DROP TABLE integration.his_payment_staging")
    op.execute("DROP TABLE integration.his_patient_staging")
