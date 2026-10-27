"""0001 — 스키마·확장·common/regulatory 테이블·권한 (design §3, §11.0).

Revision ID: 0001
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = ["common", "hr", "finance", "scm", "patient", "claims",
           "regulatory", "integration", "audit"]


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")  # fee_master EXCLUDE 제약용

    # ── common ──────────────────────────────────────────────
    op.execute("""
    CREATE TABLE common.departments (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name VARCHAR(80) NOT NULL UNIQUE,
      parent_id BIGINT REFERENCES common.departments(id),
      head_user_id BIGINT
    )""")
    op.execute("""
    CREATE TABLE common.users (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      email VARCHAR(255) NOT NULL UNIQUE,
      password_hash VARCHAR(255) NOT NULL,
      role VARCHAR(16) NOT NULL DEFAULT 'STAFF'
        CHECK (role IN ('DIRECTOR','ADMIN','MANAGER','STAFF')),
      department_id BIGINT REFERENCES common.departments(id),
      employee_id BIGINT,
      license_flag_active BOOLEAN NOT NULL DEFAULT true,
      token_version INT NOT NULL DEFAULT 0,
      failed_attempts INT NOT NULL DEFAULT 0,
      locked_until TIMESTAMPTZ,
      is_active BOOLEAN NOT NULL DEFAULT true,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE common.refresh_tokens (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES common.users(id),
      token_hash VARCHAR(64) NOT NULL UNIQUE,
      family_id VARCHAR(36) NOT NULL,
      issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      expires_at TIMESTAMPTZ NOT NULL,
      revoked_at TIMESTAMPTZ,
      replaced_by VARCHAR(64)
    )""")
    op.execute("CREATE INDEX ix_refresh_user ON common.refresh_tokens (user_id)")
    op.execute("CREATE INDEX ix_refresh_family ON common.refresh_tokens (family_id)")
    op.execute("""
    CREATE TABLE common.notifications (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      user_id BIGINT NOT NULL REFERENCES common.users(id),
      type VARCHAR(32) NOT NULL,
      title VARCHAR(200) NOT NULL,
      body TEXT,
      read_at TIMESTAMPTZ,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX ix_notif_user ON common.notifications (user_id, created_at DESC)")

    # ── audit (append-only — DB 레벨 강제는 권한 블록에서) ──
    op.execute("""
    CREATE TABLE audit.audit_log (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      actor BIGINT, action VARCHAR(32) NOT NULL,
      entity VARCHAR(64) NOT NULL, target_id VARCHAR(64),
      before JSONB, after JSONB,
      at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE audit.access_log (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      actor BIGINT NOT NULL, action VARCHAR(16) NOT NULL,
      entity VARCHAR(64) NOT NULL, target_id VARCHAR(64),
      route VARCHAR(200), at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")

    # ── regulatory ──────────────────────────────────────────
    op.execute("""
    CREATE TABLE regulatory.fee_master (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code VARCHAR(20) NOT NULL,
      code_type VARCHAR(16) NOT NULL CHECK (code_type IN ('TARIFF','DRUG','MATERIAL')),
      name TEXT NOT NULL,
      unit_price NUMERIC(12,2) NOT NULL,
      price_cap NUMERIC(12,2),
      criteria_text TEXT,
      valid_from DATE NOT NULL,
      valid_to DATE,
      source VARCHAR(8) NOT NULL DEFAULT 'API',
      synced_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_fee_version UNIQUE (code, code_type, valid_from),
      CONSTRAINT ck_fee_range CHECK (valid_to IS NULL OR valid_to > valid_from),
      CONSTRAINT ex_fee_overlap EXCLUDE USING gist (
        code WITH =, code_type WITH =,
        daterange(valid_from, COALESCE(valid_to, 'infinity'::date), '[)') WITH &&
      )
    )""")
    op.execute("CREATE INDEX ix_fee_lookup ON regulatory.fee_master (code, code_type, valid_from DESC)")
    op.execute("""
    CREATE TABLE regulatory.fee_sync_runs (
      run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      source_version VARCHAR(64) NOT NULL UNIQUE,  -- 멱등키 (design §3.2)
      started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at TIMESTAMPTZ,
      row_count INT
    )""")
    op.execute("""
    CREATE TABLE regulatory.notices (
      erp_ref_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
      notice_type VARCHAR(16) NOT NULL CHECK (notice_type IN
        ('TARIFF','DRUG_MATERIAL','REVIEW_CRITERIA','MEDICAL_LAW','ADMIN','NARCOTIC','LABOR','INTERNAL')),
      current_rev INT NOT NULL DEFAULT 1,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE regulatory.notice_revisions (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      erp_ref_id UUID NOT NULL REFERENCES regulatory.notices(erp_ref_id),
      revision_no INT NOT NULL,
      title TEXT NOT NULL,
      body_text TEXT NOT NULL,
      content_hash CHAR(64) NOT NULL,
      effective_from DATE NOT NULL,
      effective_to DATE,
      status VARCHAR(12) NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN ('SCHEDULED','ACTIVE','REPEALED')),
      created_by BIGINT NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_notice_rev UNIQUE (erp_ref_id, revision_no)
    )""")
    op.execute("""
    CREATE TABLE regulatory.ai_dispatch_queue (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      erp_ref_id UUID NOT NULL,
      revision_no INT NOT NULL,
      event_type VARCHAR(8) NOT NULL CHECK (event_type IN ('CREATE','UPDATE','REVOKE')),
      urgency VARCHAR(10) NOT NULL DEFAULT 'BATCH',
      client_request_id UUID NOT NULL DEFAULT gen_random_uuid(),
      filter_status VARCHAR(12) NOT NULL DEFAULT 'PENDING'
        CHECK (filter_status IN ('PENDING','PASSED','BLOCKED','APPROVED')),
      filter_detail JSONB,
      approved_hash CHAR(64),
      dispatch_status VARCHAR(10) NOT NULL DEFAULT 'QUEUED'
        CHECK (dispatch_status IN ('QUEUED','SENT','DONE','FAILED','DLQ')),
      retry_count INT NOT NULL DEFAULT 0,
      triggered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      sent_at TIMESTAMPTZ,
      response_code INT,
      elapsed_ms INT
    )""")

    # ── 권한 (design §6.3 — erp_app 최소권한, append-only DB 강제) ──
    # 운영 DB(기존 서비스)에서는 DBA가 계정 생성 — 여기서는 존재 시에만 GRANT
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT USAGE ON SCHEMA common, hr, finance, scm, patient, claims,
                            regulatory, integration, audit TO erp_app;
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA
          common, regulatory TO erp_app;
        GRANT SELECT, INSERT ON audit.audit_log, audit.access_log TO erp_app;
        REVOKE UPDATE, DELETE ON audit.audit_log, audit.access_log FROM erp_app;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA common, regulatory, audit TO erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
