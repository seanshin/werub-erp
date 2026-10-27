"""구매 거버넌스 — PO 예산 약정 연계·3-way 입고수량 검수 (구매 도메인 심화).

- purchase_orders: department_id·account_code(예산 귀속)·receipt_count(분할입고 채번) + status에 PARTIAL.
- po_lines: received_qty(누적 입고 — 3-way 수량 대사).
CHECK 제약은 모델명·PG자동명 둘 다 DROP IF EXISTS(0061 교훈). ADD COLUMN IF NOT EXISTS 멱등.
"""
from alembic import op

revision = "0129"
down_revision = "0128"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scm.purchase_orders ADD COLUMN IF NOT EXISTS department_id BIGINT")
    op.execute("ALTER TABLE scm.purchase_orders ADD COLUMN IF NOT EXISTS "
               "account_code VARCHAR(20) NOT NULL DEFAULT '1150'")
    op.execute("ALTER TABLE scm.purchase_orders ADD COLUMN IF NOT EXISTS "
               "receipt_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE scm.po_lines ADD COLUMN IF NOT EXISTS "
               "received_qty NUMERIC(12,2) NOT NULL DEFAULT 0")
    # status CHECK 재작성 — PARTIAL 추가(분할입고). 모델명·PG자동명 둘 다 제거 후 재생성.
    op.execute("ALTER TABLE scm.purchase_orders DROP CONSTRAINT IF EXISTS ck_po_status")
    op.execute("ALTER TABLE scm.purchase_orders DROP CONSTRAINT IF EXISTS purchase_orders_status_check")
    op.execute("ALTER TABLE scm.purchase_orders ADD CONSTRAINT ck_po_status "
               "CHECK (status IN ('REQUESTED','APPROVED','PARTIAL','RECEIVED','REJECTED'))")


def downgrade() -> None:
    op.execute("ALTER TABLE scm.purchase_orders DROP CONSTRAINT IF EXISTS ck_po_status")
    op.execute("ALTER TABLE scm.purchase_orders ADD CONSTRAINT ck_po_status "
               "CHECK (status IN ('REQUESTED','APPROVED','RECEIVED','REJECTED'))")
    op.execute("ALTER TABLE scm.po_lines DROP COLUMN IF EXISTS received_qty")
    op.execute("ALTER TABLE scm.purchase_orders DROP COLUMN IF EXISTS receipt_count")
    op.execute("ALTER TABLE scm.purchase_orders DROP COLUMN IF EXISTS account_code")
    op.execute("ALTER TABLE scm.purchase_orders DROP COLUMN IF EXISTS department_id")
