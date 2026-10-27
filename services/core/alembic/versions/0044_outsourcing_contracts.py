"""0044 — 위수탁 계약 관리: scm.outsourcing_contracts (전체검토 공백 해소).

급식·세탁·의료폐기물·보안 등 위탁계약의 기간·금액·갱신을 추적하고,
월 위탁료를 미지급금(payables)으로 발생시켜 지급·세금계산서 대사 흐름과 연결.

Revision ID: 0044
"""
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE scm.outsourcing_contracts (
            id BIGSERIAL PRIMARY KEY,
            vendor_name VARCHAR(120) NOT NULL,
            supplier_id BIGINT REFERENCES scm.suppliers(id),
            category VARCHAR(12) NOT NULL CHECK (category IN
                ('MEAL','LAUNDRY','WASTE','SECURITY','CLEANING','FACILITY','IT','OTHER')),
            title VARCHAR(200) NOT NULL,
            monthly_amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            start_on DATE NOT NULL,
            end_on DATE NOT NULL,
            auto_renew BOOLEAN NOT NULL DEFAULT false,
            status VARCHAR(12) NOT NULL DEFAULT 'ACTIVE'
                CHECK (status IN ('ACTIVE','EXPIRED','TERMINATED')),
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_oc_end ON scm.outsourcing_contracts (end_on)")


def downgrade() -> None:
    op.execute("DROP TABLE scm.outsourcing_contracts")
