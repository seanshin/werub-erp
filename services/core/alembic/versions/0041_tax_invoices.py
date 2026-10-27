"""0041 — 전자세금계산서 수취·대사: finance.tax_invoices (전체검토 A-1).

지급 전 증빙 통제: 수취 세금계산서(과세)/계산서(면세) 등록(수기·홈택스 CSV) →
미지급금(payables) 대사(금액 검증) → 미대사·미증빙 가시화.
의료기관 = 면세사업 중심이라 매입 VAT는 원칙 불공제(원가) — 과세/면세 구분 집계가
사업장현황신고·부가세(A-2)의 기초.

Revision ID: 0041
"""
from alembic import op

revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE finance.tax_invoices (
            id BIGSERIAL PRIMARY KEY,
            approval_no VARCHAR(30) NOT NULL UNIQUE,
            invoice_type VARCHAR(8) NOT NULL DEFAULT 'TAX'
                CHECK (invoice_type IN ('TAX','EXEMPT','ZERO')),
            direction VARCHAR(3) NOT NULL DEFAULT 'IN' CHECK (direction IN ('IN','OUT')),
            issue_date DATE NOT NULL,
            supplier_bizno VARCHAR(13) NOT NULL,
            supplier_name VARCHAR(120),
            supply_amount NUMERIC(14,0) NOT NULL,
            vat_amount NUMERIC(14,0) NOT NULL DEFAULT 0,
            total_amount NUMERIC(14,0) NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'RECEIVED'
                CHECK (status IN ('RECEIVED','MATCHED','VOID')),
            payable_id BIGINT REFERENCES finance.payables(id),
            note TEXT,
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_taxinv_status ON finance.tax_invoices (status)")
    op.execute("CREATE INDEX ix_taxinv_issue ON finance.tax_invoices (issue_date)")


def downgrade() -> None:
    op.execute("DROP TABLE finance.tax_invoices")
