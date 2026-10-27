"""0074 — 검진권 전기 규칙 테이블 + 시드 기본규칙(관리자 설정 가능).

수수료·정산 처리가 기관·계약별로 달라 하드코딩 대신 데이터 규칙으로 관리.
시드는 현 B-G1-B 기본 동작 보존(+settlement) — 관리자가 /admin에서 조정.

Revision ID: 0074
"""
from alembic import op
import sqlalchemy as sa

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "voucher_posting_rules",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  primary_key=True, autoincrement=True),
        sa.Column("line_type", sa.String(16), nullable=False, index=True),
        sa.Column("dealer_type", sa.String(12)),
        sa.Column("debit_account", sa.String(20)),
        sa.Column("credit_account", sa.String(20)),
        sa.Column("amount_source", sa.String(120)),
        sa.Column("reverse_on_negative", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("skip", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("priority", sa.Integer, nullable=False, server_default="100"),
        sa.Column("description", sa.String(200)),
        sa.Column("updated_by", sa.BigInteger),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(
            "line_type IN ('purchase','sub_purchase','resale','redeem','topup','refund',"
            "'commission','settlement')", name="ck_vrule_line"),
        schema="finance",
    )
    # 시드 기본규칙 — 현 동작 보존(dealer_type NULL=전체). 관리자 조정 전제.
    op.execute("""
        INSERT INTO finance.voucher_posting_rules
            (line_type, debit_account, credit_account, amount_source,
             reverse_on_negative, skip, active, priority, description)
        VALUES
            ('purchase','1110','2140','totalAmount,amount', false, false, true, 100,
             '딜러 선결제 매입 → 선수금(Dr 현금/Cr 2140)'),
            ('sub_purchase', NULL, NULL, NULL, false, true, true, 100,
             '딜러간 하위배분 — 병원 무전표'),
            ('resale', NULL, NULL, NULL, false, true, true, 100,
             '딜러→환자 재판매 — 병원 무전표(딜러 장부)'),
            ('redeem','2140','4110','amountUsed,amount', false, false, true, 100,
             '검진권 사용 → 선수금 해소·수익(Dr 2140/Cr 4110)'),
            ('topup','1110','4110','amount', false, false, true, 100,
             '액면초과 추가청구(Dr 현금/Cr 4110)'),
            ('refund','2140','1110','refundAmount,amount', false, false, true, 100,
             '소비자 환급(Dr 2140/Cr 현금)'),
            ('commission','5520','2110','commissionAmount', true, false, true, 100,
             '수수료 거래별 적립(Dr 5520/Cr 2110)·음수=clawback 반전 [관리자 방향 조정]'),
            ('settlement','2110','1110','netPayableKrw,netPayable', false, false, true, 100,
             '딜러 정산 지급(Dr 2110/Cr 현금) [관리자 AP/AR 조정]')
    """)


def downgrade() -> None:
    op.drop_table("voucher_posting_rules", schema="finance")
