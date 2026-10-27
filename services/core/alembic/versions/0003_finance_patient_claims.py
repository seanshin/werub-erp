"""0003 — finance·patient·claims·integration 스키마 테이블 (design §3, M4).

Revision ID: 0003
"""
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── finance ─────────────────────────────────────────────
    op.execute("""
    CREATE TABLE finance.accounts (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code VARCHAR(20) NOT NULL UNIQUE,
      name VARCHAR(80) NOT NULL,
      kind VARCHAR(10) NOT NULL,
      parent_id BIGINT REFERENCES finance.accounts(id)
    )""")
    op.execute("""
    CREATE TABLE finance.vouchers (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      voucher_on DATE NOT NULL,
      source_module VARCHAR(16) NOT NULL,
      source_ref VARCHAR(64) NOT NULL,
      memo TEXT,
      status VARCHAR(10) NOT NULL DEFAULT 'POSTED' CHECK (status IN ('POSTED','REVERSED')),
      reversed_by BIGINT, reverses BIGINT,
      created_by BIGINT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_voucher_source UNIQUE (source_module, source_ref)
    )""")
    op.execute("""
    CREATE TABLE finance.voucher_lines (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      voucher_id BIGINT NOT NULL REFERENCES finance.vouchers(id),
      account_code VARCHAR(20) NOT NULL,
      side VARCHAR(6) NOT NULL CHECK (side IN ('DEBIT','CREDIT')),
      amount NUMERIC(14,0) NOT NULL,
      department_id BIGINT,
      detail VARCHAR(200)
    )""")
    op.execute("CREATE INDEX ix_vline_voucher ON finance.voucher_lines (voucher_id)")
    op.execute("""
    CREATE TABLE finance.budgets (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      department_id BIGINT NOT NULL,
      fiscal_year INT NOT NULL,
      account_code VARCHAR(20) NOT NULL,
      amount NUMERIC(14,0) NOT NULL,
      CONSTRAINT uq_budget UNIQUE (department_id, fiscal_year, account_code)
    )""")
    # 기본 계정과목 seed (수납·삭감 흐름용)
    op.execute("""
    INSERT INTO finance.accounts (code, name, kind) VALUES
      ('1110','현금성자산','ASSET'), ('1140','보험미수금','ASSET'),
      ('4110','외래수익','REVENUE'), ('5910','삭감손실','EXPENSE')
    """)

    # ── patient ─────────────────────────────────────────────
    op.execute("""
    CREATE TABLE patient.patients (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      chart_no VARCHAR(20) NOT NULL UNIQUE,
      name VARCHAR(80) NOT NULL,
      birth_date DATE NOT NULL,
      phone_enc BYTEA,
      phone_bidx VARCHAR(64),
      last_visit_on DATE,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_patient_name ON patient.patients (name)")
    op.execute("CREATE INDEX ix_patient_bidx ON patient.patients (phone_bidx)")
    op.execute("""
    CREATE TABLE patient.receptions (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      patient_id BIGINT NOT NULL REFERENCES patient.patients(id),
      dept_code VARCHAR(10) NOT NULL,
      doctor_id BIGINT,
      received_on DATE NOT NULL,
      source VARCHAR(8) NOT NULL DEFAULT 'MANUAL',
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_reception_date ON patient.receptions (received_on)")
    op.execute("""
    CREATE TABLE patient.payments (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      reception_id BIGINT NOT NULL REFERENCES patient.receptions(id),
      amount NUMERIC(14,0) NOT NULL,
      method VARCHAR(10) NOT NULL DEFAULT 'CARD',
      paid_on DATE NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'PAID' CHECK (status IN ('PAID','CANCELED')),
      cancels_id BIGINT,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_payment_date ON patient.payments (paid_on)")
    op.execute("""
    CREATE TABLE patient.reservations (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      patient_id BIGINT NOT NULL REFERENCES patient.patients(id),
      dept_code VARCHAR(10) NOT NULL,
      doctor_id BIGINT NOT NULL,
      reserved_at TIMESTAMPTZ NOT NULL,
      status VARCHAR(10) NOT NULL DEFAULT 'BOOKED'
        CHECK (status IN ('BOOKED','VISITED','NOSHOW','CANCELED')),
      CONSTRAINT uq_reservation_slot UNIQUE (doctor_id, reserved_at)
    )""")
    op.execute("""
    CREATE TABLE patient.daily_closings (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      closing_on DATE NOT NULL,
      seq INT NOT NULL DEFAULT 1,
      total_amount NUMERIC(14,0) NOT NULL,
      payment_count INT NOT NULL,
      voucher_ref VARCHAR(64),
      status VARCHAR(10) NOT NULL DEFAULT 'CLOSED' CHECK (status IN ('CLOSED','AMENDED')),
      closed_by BIGINT NOT NULL,
      closed_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_closing_date ON patient.daily_closings (closing_on)")

    # ── claims + integration ────────────────────────────────
    op.execute("""
    CREATE TABLE integration.hira_notice_staging (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      stored_name VARCHAR(64) NOT NULL UNIQUE,
      original_name VARCHAR(200) NOT NULL,
      size_bytes BIGINT NOT NULL,
      uploaded_by BIGINT NOT NULL,
      status VARCHAR(12) NOT NULL DEFAULT 'NEW'
        CHECK (status IN ('NEW','OK','FAILED','REPROCESSED')),
      error_detail JSONB,
      received_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE claims.claim_batches (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      receipt_no VARCHAR(20) NOT NULL UNIQUE,
      care_month VARCHAR(7) NOT NULL,
      decided_on DATE,
      notice_arrived_on DATE NOT NULL,
      claimed_amt NUMERIC(14,0), decided_amt NUMERIC(14,0),
      staging_id BIGINT
    )""")
    op.execute("""
    CREATE TABLE claims.claim_statements (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      batch_id BIGINT NOT NULL REFERENCES claims.claim_batches(id),
      stmt_serial VARCHAR(10) NOT NULL,
      chart_no VARCHAR(20),
      dept_code VARCHAR(10),
      doctor_id BIGINT,
      claimed_amt NUMERIC(14,0), adjusted_amt NUMERIC(14,0), decided_amt NUMERIC(14,0),
      CONSTRAINT uq_stmt UNIQUE (batch_id, stmt_serial)
    )""")
    op.execute("""
    CREATE TABLE claims.claim_adjustments (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      statement_id BIGINT NOT NULL REFERENCES claims.claim_statements(id),
      adj_code VARCHAR(10),
      adj_category VARCHAR(20),
      adj_amt NUMERIC(14,0) NOT NULL,
      adj_reason TEXT,
      map_status VARCHAR(10) NOT NULL DEFAULT 'UNMAPPED'
        CHECK (map_status IN ('AUTO','MANUAL','UNMAPPED'))
    )""")
    op.execute("""
    CREATE TABLE claims.appeals (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      adjustment_id BIGINT NOT NULL REFERENCES claims.claim_adjustments(id),
      appeal_type VARCHAR(16) NOT NULL CHECK (appeal_type IN ('REVIEW_REQUEST','OBJECTION')),
      status VARCHAR(12) NOT NULL DEFAULT 'RECEIVED'
        CHECK (status IN ('RECEIVED','SUPPLEMENT','REJECTED','ACCEPTED')),
      notice_arrived_on DATE NOT NULL,
      legal_deadline DATE NOT NULL,
      final_loss_amt NUMERIC(14,0),
      voucher_ref VARCHAR(64),
      created_by BIGINT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_appeal_deadline ON claims.appeals (legal_deadline)")

    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
          finance, patient, claims, integration TO erp_app;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA finance, patient, claims, integration TO erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    for schema, tables in [
        ("claims", ["appeals", "claim_adjustments", "claim_statements", "claim_batches"]),
        ("integration", ["hira_notice_staging"]),
        ("patient", ["daily_closings", "reservations", "payments", "receptions", "patients"]),
        ("finance", ["budgets", "voucher_lines", "vouchers", "accounts"]),
    ]:
        for t in tables:
            op.execute(f"DROP TABLE IF EXISTS {schema}.{t}")
