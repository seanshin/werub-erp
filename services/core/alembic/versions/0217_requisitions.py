"""0217 — 구매요청(PR)·내부 자재청구: 요청→승인→실행 앞단 (내부운영 갭 감사 Tier2).

audit 발견: PO가 앞단(수요 청구) 없이 REQUESTED로 바로 시작·재고 출고(issue)는 무승인 즉시 OUT.
보완: purchase_requisitions/requisition_lines(구매요청→승인→PO 전환)·material_requests(자재청구→승인→불출).

Revision ID: 0217
"""
import sqlalchemy as sa
from alembic import op

revision = "0217"
down_revision = "0216"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "purchase_requisitions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("req_no", sa.String(24), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("requester", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("needed_by", sa.Date(), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="PENDING", index=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("po_id", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("req_no", name="uq_preq_reqno"),
        sa.CheckConstraint("status IN ('PENDING','APPROVED','REJECTED','CONVERTED','CANCELED')",
                           name="ck_preq_status"),
        schema="scm",
    )
    op.create_table(
        "requisition_lines",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("req_id", sa.BigInteger(),
                  sa.ForeignKey("scm.purchase_requisitions.id"), nullable=False, index=True),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("est_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("note", sa.String(200), nullable=True),
        schema="scm",
    )
    op.create_table(
        "material_requests",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("req_no", sa.String(24), nullable=False),
        sa.Column("department_id", sa.BigInteger(), nullable=True, index=True),
        sa.Column("requester", sa.BigInteger(), nullable=False),
        sa.Column("item_id", sa.BigInteger(), sa.ForeignKey("scm.items.id"), nullable=False,
                  index=True),
        sa.Column("qty", sa.Numeric(12, 2), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="REQUESTED", index=True),
        sa.Column("approved_by", sa.BigInteger(), nullable=True),
        sa.Column("issued_on", sa.Date(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("req_no", name="uq_mreq_reqno"),
        sa.CheckConstraint("status IN ('REQUESTED','APPROVED','ISSUED','REJECTED','CANCELED')",
                           name="ck_mreq_status"),
        schema="scm",
    )


def downgrade() -> None:
    op.drop_table("material_requests", schema="scm")
    op.drop_table("requisition_lines", schema="scm")
    op.drop_table("purchase_requisitions", schema="scm")
