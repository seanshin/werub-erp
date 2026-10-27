"""0030 — 리스 회계(K-IFRS 16): 사용권자산·리스부채 + 계정.

리스개시: 리스부채(지급액 현가)·사용권자산 인식. 매월: 이자(리스부채×율)·원금상환·ROU 상각.

Revision ID: 0030
"""
from alembic import op

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None

_ACCOUNTS = [
    ("1720", "사용권자산", "ASSET"),
    ("1825", "사용권자산상각누계액", "ASSET"),
    ("2210", "리스부채", "LIABILITY"),
    ("5630", "리스이자비용", "EXPENSE"),
    ("5640", "사용권자산상각비", "EXPENSE"),
]


def upgrade() -> None:
    for code, name, kind in _ACCOUNTS:
        op.execute(f"""
            INSERT INTO finance.accounts (code, name, kind)
            SELECT '{code}', '{name}', '{kind}'
            WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '{code}')
        """)
    op.execute("""
        CREATE TABLE finance.lease_contracts (
            id BIGSERIAL PRIMARY KEY,
            lease_no VARCHAR(30) NOT NULL UNIQUE,
            asset_name VARCHAR(120) NOT NULL,
            lessor VARCHAR(120),
            start_on DATE NOT NULL,
            term_months INTEGER NOT NULL,
            monthly_payment NUMERIC(16,0) NOT NULL,
            annual_rate NUMERIC(6,4) NOT NULL DEFAULT 0,
            rou_initial NUMERIC(16,0) NOT NULL,
            liability_initial NUMERIC(16,0) NOT NULL,
            current_liability NUMERIC(16,0) NOT NULL,
            accumulated_amort NUMERIC(16,0) NOT NULL DEFAULT 0,
            periods_accrued INTEGER NOT NULL DEFAULT 0,
            status VARCHAR(8) NOT NULL DEFAULT 'ACTIVE',
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT ck_lease_status CHECK (status IN ('ACTIVE','ENDED'))
        )
    """)
    op.execute("""
        CREATE TABLE finance.lease_accruals (
            id BIGSERIAL PRIMARY KEY,
            lease_id BIGINT NOT NULL REFERENCES finance.lease_contracts(id),
            period VARCHAR(7) NOT NULL,
            interest NUMERIC(16,0) NOT NULL,
            principal NUMERIC(16,0) NOT NULL,
            amortization NUMERIC(16,0) NOT NULL,
            voucher_ref VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_lease_accrual UNIQUE (lease_id, period)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE finance.lease_accruals")
    op.execute("DROP TABLE finance.lease_contracts")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('1720','1825','2210','5630','5640')")
