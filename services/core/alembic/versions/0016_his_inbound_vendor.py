"""0016 — integration.his_inbound_events(웹훅 인박스) + scm.suppliers.external_vendor_code.

HIS↔ERP 마스터플랜 대비 선반영: HIS 아웃바운드 웹훅 멱등 수신 인박스 + 거래처 코드 매핑.

Revision ID: 0016
"""
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration.his_inbound_events (
            id BIGSERIAL PRIMARY KEY,
            domain VARCHAR(24) NOT NULL,
            event_type VARCHAR(48) NOT NULL,
            source_ref VARCHAR(120) NOT NULL,
            payload JSONB NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'NEW'
                CHECK (status IN ('NEW','APPLIED','FAILED')),
            error VARCHAR(300),
            received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_his_inbound_source_ref UNIQUE (source_ref)
        )
    """)
    op.execute("CREATE INDEX ix_his_inbound_domain ON integration.his_inbound_events (domain)")
    op.execute("ALTER TABLE scm.suppliers ADD COLUMN external_vendor_code VARCHAR(40)")
    op.execute("CREATE INDEX ix_supplier_vendor_code ON scm.suppliers (external_vendor_code) "
               "WHERE external_vendor_code IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS integration.his_inbound_events")
    op.execute("DROP INDEX IF EXISTS scm.ix_supplier_vendor_code")
    op.execute("ALTER TABLE scm.suppliers DROP COLUMN external_vendor_code")
