"""0004 — scm 스키마 테이블 + 마약류 append-only DB 강제 (design §3.4, M5).

Revision ID: 0004
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE scm.suppliers (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      name VARCHAR(120) NOT NULL,
      business_no VARCHAR(12) NOT NULL UNIQUE,
      contact VARCHAR(120)
    )""")
    op.execute("""
    CREATE TABLE scm.items (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      code VARCHAR(20) NOT NULL UNIQUE,
      name VARCHAR(120) NOT NULL,
      category VARCHAR(12) NOT NULL CHECK (category IN ('DRUG','SUPPLY','EQUIPMENT')),
      unit VARCHAR(10) NOT NULL DEFAULT 'EA',
      is_narcotic BOOLEAN NOT NULL DEFAULT false,
      safety_stock_qty NUMERIC(12,2) NOT NULL DEFAULT 0
    )""")
    op.execute("""
    CREATE TABLE scm.lots (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      item_id BIGINT NOT NULL REFERENCES scm.items(id),
      lot_no VARCHAR(40) NOT NULL,
      expiry_on DATE NOT NULL,
      received_on DATE NOT NULL,
      received_qty NUMERIC(12,2) NOT NULL,
      remaining_qty NUMERIC(12,2) NOT NULL CHECK (remaining_qty >= 0),
      supplier_id BIGINT,
      CONSTRAINT uq_lot UNIQUE (item_id, lot_no)
    )""")
    op.execute("CREATE INDEX ix_lot_fifo ON scm.lots (item_id, expiry_on, received_on)")
    op.execute("""
    CREATE TABLE scm.stock_movements (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      item_id BIGINT NOT NULL REFERENCES scm.items(id),
      lot_id BIGINT NOT NULL REFERENCES scm.lots(id),
      movement_type VARCHAR(8) NOT NULL CHECK (movement_type IN ('IN','OUT','DISPOSE','ADJUST')),
      qty NUMERIC(12,2) NOT NULL,
      moved_on DATE NOT NULL,
      actor BIGINT NOT NULL,
      override_reason VARCHAR(200),
      created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    op.execute("""
    CREATE TABLE scm.narcotics_ledger (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      seq_no BIGINT NOT NULL,
      item_id BIGINT NOT NULL REFERENCES scm.items(id),
      lot_id BIGINT,
      entry_type VARCHAR(10) NOT NULL
        CHECK (entry_type IN ('IN','OUT','DISPOSE','ADJUST','CORRECTION')),
      qty NUMERIC(12,2) NOT NULL,
      balance_after NUMERIC(12,2) NOT NULL,
      handler_id BIGINT NOT NULL,
      corrects_id BIGINT REFERENCES scm.narcotics_ledger(id),
      correction_reason TEXT,
      row_hash CHAR(64) NOT NULL,
      created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      CONSTRAINT uq_narc_seq UNIQUE (item_id, seq_no)
    )""")
    op.execute("""
    CREATE TABLE scm.purchase_orders (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      supplier_id BIGINT NOT NULL REFERENCES scm.suppliers(id),
      status VARCHAR(12) NOT NULL DEFAULT 'REQUESTED'
        CHECK (status IN ('REQUESTED','APPROVED','RECEIVED','REJECTED')),
      requested_by BIGINT NOT NULL,
      approved_by BIGINT,
      requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
      received_at TIMESTAMPTZ
    )""")
    op.execute("""
    CREATE TABLE scm.po_lines (
      id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      po_id BIGINT NOT NULL REFERENCES scm.purchase_orders(id),
      item_id BIGINT NOT NULL REFERENCES scm.items(id),
      qty NUMERIC(12,2) NOT NULL,
      unit_price NUMERIC(12,2) NOT NULL
    )""")
    op.execute("""
    DO $$ BEGIN
      IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'erp_app') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA scm TO erp_app;
        GRANT USAGE ON ALL SEQUENCES IN SCHEMA scm TO erp_app;
        -- 마약류 원장 append-only — UPDATE/DELETE 차단 (design §3.4)
        REVOKE UPDATE, DELETE ON scm.narcotics_ledger FROM erp_app;
      END IF;
    END $$""")


def downgrade() -> None:
    for t in ["po_lines", "purchase_orders", "narcotics_ledger", "stock_movements",
              "lots", "items", "suppliers"]:
        op.execute(f"DROP TABLE IF EXISTS scm.{t}")
