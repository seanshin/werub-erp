"""0076 — 원천징수 요율(시점 버전관리) + 예수금 계정(2150).

세법 정기 개정 대응 — 요율을 코드 아닌 시행일 구간 설정으로 관리. 검진권 딜러 정산 시
시점 적용 요율로 재계산. 시드 없음(요율 미정·DB-1 대기 → 빈 상태=현행 무원천징수 유지).

Revision ID: 0076
"""
from alembic import op
import sqlalchemy as sa

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('2150','예수금(원천징수)','LIABILITY')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "withholding_rates",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("dealer_type", sa.String(12)),               # DOMESTIC|NULL=전체
        sa.Column("rate_pct", sa.Numeric(6, 3), nullable=False),  # 예 3.300
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("effective_to", sa.Date),                    # NULL=현행
        sa.Column("withholding_account", sa.String(20), nullable=False, server_default="2150"),
        sa.Column("tax_invoice_by", sa.String(20)),            # 병원역발행|딜러발행 등(메모)
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("note", sa.String(200)),
        sa.Column("updated_by", sa.BigInteger),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="finance",
    )
    op.create_index("ix_withholding_eff", "withholding_rates",
                    ["dealer_type", "effective_from"], schema="finance")


def downgrade() -> None:
    op.drop_table("withholding_rates", schema="finance")
