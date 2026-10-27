"""성능 인덱스 — finance.vouchers 집계 풀스캔 제거 (전체검토 Tier 2).

vouchers는 환자청구·claims·scm 전표가 누적되는 최대 성장 테이블인데 voucher_on·status에
인덱스가 없어 재무제표(statements)·경영분석·감사(audit_service) 집계가 매 호출 seq_scan.
긴 스캔이 공유 PG(max_connections=200) 슬롯을 길게 점유 → 슬롯 경쟁 인시던트와 직결.

- ix_voucher_on: 범위만 필터하는 statements(_by_account: voucher_on>=since/<=until).
- ix_voucher_status_on: status 동반 필터(audit_service status='POSTED', period_service
  status IN('DRAFT','PENDING'))의 합성 인덱스.

대상 prod 데이터 아직 소규모라 CONCURRENTLY 불요. downgrade에서 drop.
참조: 전체검토 2026-06-27 성능 갈래.
"""
from alembic import op

revision = "0120"
down_revision = "0119"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_voucher_on", "vouchers", ["voucher_on"], schema="finance")
    op.create_index("ix_voucher_status_on", "vouchers", ["status", "voucher_on"],
                    schema="finance")


def downgrade() -> None:
    op.drop_index("ix_voucher_status_on", table_name="vouchers", schema="finance")
    op.drop_index("ix_voucher_on", table_name="vouchers", schema="finance")
