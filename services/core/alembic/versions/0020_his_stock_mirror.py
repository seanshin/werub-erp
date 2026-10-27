"""0020 — integration.his_stock_mirror (HIS 재고 이벤트 미러).

D-4: HIS stock/inventory(lot 없는 수량)를 ERP FIFO와 분리 적재 — 재고 대사·가시성.

Revision ID: 0020
"""
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE integration.his_stock_mirror (
            id BIGSERIAL PRIMARY KEY,
            item_code VARCHAR(40) NOT NULL,
            erp_item_code VARCHAR(40),
            name VARCHAR(120),
            movement_type VARCHAR(12) NOT NULL,
            qty NUMERIC(14,2) NOT NULL,
            balance_after NUMERIC(14,2),
            ref VARCHAR(60),
            source VARCHAR(12) NOT NULL,
            moved_on DATE NOT NULL,
            received_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX ix_his_stock_item ON integration.his_stock_mirror (item_code)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS integration.his_stock_mirror")
