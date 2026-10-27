"""0028 — 재고 실사(stock-taking) 조정 + 재고감모손실·조정이익 계정.

실사: 실재고 입력 → 장부재고 대비 차이 → 로트 ADJUST + 재고감모손실(5530)/조정이익(4620) 전표.

Revision ID: 0028
"""
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for code, name, kind in [("5530", "재고감모손실", "EXPENSE"),
                             ("4620", "재고조정이익", "REVENUE")]:
        op.execute(f"""
            INSERT INTO finance.accounts (code, name, kind)
            SELECT '{code}', '{name}', '{kind}'
            WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '{code}')
        """)
    op.execute("""
        CREATE TABLE scm.stock_takes (
            id BIGSERIAL PRIMARY KEY,
            item_id BIGINT NOT NULL REFERENCES scm.items(id),
            taken_on DATE NOT NULL,
            book_qty NUMERIC(12,2) NOT NULL,
            physical_qty NUMERIC(12,2) NOT NULL,
            diff_qty NUMERIC(12,2) NOT NULL,
            unit_cost NUMERIC(14,2) NOT NULL DEFAULT 0,
            diff_value NUMERIC(16,0) NOT NULL DEFAULT 0,
            status VARCHAR(8) NOT NULL DEFAULT 'DRAFT',
            voucher_ref VARCHAR(64),
            note VARCHAR(200),
            created_by BIGINT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            applied_at TIMESTAMPTZ,
            CONSTRAINT ck_stocktake_status CHECK (status IN ('DRAFT','APPLIED'))
        )
    """)
    op.execute("CREATE INDEX ix_stocktake_item ON scm.stock_takes (item_id, taken_on)")


def downgrade() -> None:
    op.execute("DROP TABLE scm.stock_takes")
    op.execute("DELETE FROM finance.accounts WHERE code IN ('5530','4620')")
