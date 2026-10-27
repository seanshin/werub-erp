"""0214 — 입고 검수·반품(RMA·내부운영 갭 감사 Tier1): RETURN 이동 + purchase_returns.

audit 발견: StockMovement 타입에 RETURN 없음·receive는 수량 3-way만·공급사 반품/credit note 전무
(냉장약품 온도일탈·유효기간 임박 도착·파손 의료재료 반품 경로 부재). 보완:
- ck_mv_type에 'RETURN' 추가(로트 잔량 차감 이동).
- scm.purchase_returns: 반품 사유·수량·금액·credit note(DR 2110/CR 1150) 추적.

Revision ID: 0214
"""
import sqlalchemy as sa
from alembic import op

revision = "0214"
down_revision = "0213"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0004_scm.py가 인라인 무명 CHECK로 생성 → PG 자동명 stock_movements_movement_type_check
    # (모델명 ck_mv_type 아님). 양쪽 IF EXISTS 드롭 후 모델명으로 재생성(이후 마이그 일관).
    op.execute("ALTER TABLE scm.stock_movements "
               "DROP CONSTRAINT IF EXISTS stock_movements_movement_type_check")
    op.execute("ALTER TABLE scm.stock_movements DROP CONSTRAINT IF EXISTS ck_mv_type")
    op.create_check_constraint(
        "ck_mv_type", "stock_movements",
        "movement_type IN ('IN','OUT','DISPOSE','ADJUST','RETURN')", schema="scm")
    op.create_table(
        "purchase_returns",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("po_id", sa.BigInteger(),
                  sa.ForeignKey("scm.purchase_orders.id"), nullable=False, index=True),
        sa.Column("supplier_id", sa.BigInteger(), nullable=True),
        sa.Column("item_id", sa.BigInteger(),
                  sa.ForeignKey("scm.items.id"), nullable=False, index=True),
        sa.Column("lot_id", sa.BigInteger(),
                  sa.ForeignKey("scm.lots.id"), nullable=False, index=True),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("unit_cost", sa.Numeric(14, 2), nullable=False),
        sa.Column("amount", sa.Numeric(16, 0), nullable=False),
        sa.Column("reason", sa.String(12), nullable=False, server_default="DEFECT"),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("returned_on", sa.Date(), nullable=False, index=True),
        sa.Column("credit_voucher_ref", sa.String(64), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("reason IN ('DEFECT','DAMAGE','EXPIRY','WRONG_ITEM','OTHER')",
                           name="ck_pret_reason"),
        schema="scm",
    )


def downgrade() -> None:
    op.drop_table("purchase_returns", schema="scm")
    op.execute("DELETE FROM scm.stock_movements WHERE movement_type = 'RETURN'")
    op.execute("ALTER TABLE scm.stock_movements DROP CONSTRAINT IF EXISTS ck_mv_type")
    op.create_check_constraint(
        "ck_mv_type", "stock_movements",
        "movement_type IN ('IN','OUT','DISPOSE','ADJUST')", schema="scm")
