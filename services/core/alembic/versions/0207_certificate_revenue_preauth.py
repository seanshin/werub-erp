"""0207 — HIS e결재 회계연계: 제증명수입 계정(4120) + 사전승인 고가오더 관리회계(preauth_orders).

HIS relay 회신(사용자 결정):
- 제증명(진단서·소견서) 수입을 **신규 4120 제증명수입**으로 분리(진료수익 4110과 구분·손익 가시성).
- 사전승인 고가오더(preauth.order_approved)는 **관리회계 리포트만**(GL 미전기·약정 단계). category_summary
  는 ERP가 preauth_orders에서 카테고리별 집계 제공.

Revision ID: 0207
"""
import sqlalchemy as sa
from alembic import op

revision = "0207"
down_revision = "0206"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('4120','제증명수입','REVENUE')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "preauth_orders",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("order_ref", sa.String(64), nullable=False),
        sa.Column("category", sa.String(40), nullable=False, server_default="UNCLASSIFIED"),
        sa.Column("amount", sa.Numeric(16, 0), nullable=False),
        sa.Column("dept_code", sa.String(10), nullable=True),
        sa.Column("approver", sa.String(80), nullable=True),
        sa.Column("approved_on", sa.Date(), nullable=False, index=True),
        sa.Column("note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("order_ref", name="uq_preauth_order"),
        schema="finance",
    )
    op.create_index("ix_preauth_cat_on", "preauth_orders", ["category", "approved_on"],
                    schema="finance")


def downgrade() -> None:
    op.drop_index("ix_preauth_cat_on", "preauth_orders", schema="finance")
    op.drop_table("preauth_orders", schema="finance")
    op.execute("DELETE FROM finance.accounts WHERE code = '4120'")
