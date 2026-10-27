"""0185 — IT 비용 GL 전표 전기 준비.

itam.service_costs.posted_at(전기 시각·재전기 방지) + 신규 비용계정 5525 정보시스템운영비.

계정 매핑(결정): IT 서비스 월 비용(SaaS·클라우드·AI·구독·SW사용료)은 취득이 아닌 반복 운영비 →
전액 OPEX. DR 5525 정보시스템운영비 / CR 2110 미지급금. 의료기관 면세라 매입세액 불공제 →
부가세 포함액 전액 비용(부가세대급금 분리 없음). 구매SW 자본화는 무형자산 연결(F-5)로 별도.

Revision ID: 0185
"""
import sqlalchemy as sa
from alembic import op

revision = "0185"
down_revision = "0184"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_costs", sa.Column("posted_at", sa.DateTime(timezone=True)),
                  schema="itam")
    # 신규 판관비 계정 — IT 정보시스템 운영비(고정비·SGA). 멱등.
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind, active, cost_type, cost_function)
        SELECT '5525', '정보시스템운영비', 'EXPENSE', true, 'FIXED', 'SGA'
        WHERE NOT EXISTS (SELECT 1 FROM finance.accounts WHERE code = '5525')
    """)


def downgrade() -> None:
    op.execute("DELETE FROM finance.accounts WHERE code = '5525'")
    op.drop_column("service_costs", "posted_at", schema="itam")
