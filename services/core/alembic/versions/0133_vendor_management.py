"""벤더 관리 — 공급사 상태 생애주기·평가 스코어카드 (구매 도메인 심화).

suppliers.status(등록심사 생애주기)·eval_score(최근 평가) + vendor_evaluations(기간별 납기·품질·단가 점수).
기존 공급사는 APPROVED 기본(무중단). ON CONFLICT/IF NOT EXISTS 멱등.
"""
from alembic import op
import sqlalchemy as sa

revision = "0133"
down_revision = "0132"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE scm.suppliers ADD COLUMN IF NOT EXISTS "
               "status VARCHAR(12) NOT NULL DEFAULT 'APPROVED'")
    op.execute("ALTER TABLE scm.suppliers ADD COLUMN IF NOT EXISTS eval_score NUMERIC(4,1)")
    op.create_table(
        "vendor_evaluations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("supplier_id", sa.BigInteger(), sa.ForeignKey("scm.suppliers.id"), nullable=False),
        sa.Column("period", sa.String(7), nullable=False),       # yyyy-mm
        sa.Column("delivery_score", sa.Numeric(4, 1), nullable=False),   # 납기준수 0~100
        sa.Column("quality_score", sa.Numeric(4, 1), nullable=False),    # 검수품질 0~100
        sa.Column("price_score", sa.Numeric(4, 1), nullable=False),      # 단가경쟁 0~100
        sa.Column("total", sa.Numeric(4, 1), nullable=False),
        sa.Column("note", sa.String(300)),
        sa.Column("created_by", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("supplier_id", "period", name="uq_vendor_eval"),
        schema="scm",
    )


def downgrade() -> None:
    op.drop_table("vendor_evaluations", schema="scm")
    op.execute("ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS eval_score")
    op.execute("ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS status")
