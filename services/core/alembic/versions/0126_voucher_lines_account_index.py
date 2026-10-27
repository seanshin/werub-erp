"""성능 인덱스 — finance.voucher_lines.account_code 계정별 집계 풀스캔 제거 (전체검토 2026-06-29).

voucher_lines는 환자청구·claims·scm 전표 라인이 누적되는 최대 성장 테이블인데 account_code에
인덱스가 없어, 계정별 GL 잔액 집계가 풀스캔이었다. 이 패턴은 IFRS/결산 엔진 9종(대손·이연법인세·
외화·재고저가·자산/무형 증감·재무제표 현금흐름)에 공통(allowance/deferred_tax/forex/disclosure/
settlement/statements 서비스의 `WHERE account_code=? AND voucher_on<=as_of` 집계). 결산 as_of는
기말이라 voucher_on 술어가 비선택적 → account_code 술어를 풀려면 전 스캔 외 방법이 없었다.

(account_code, voucher_id) 복합 — account_code 필터 + vouchers 조인(voucher_id)을 함께 커버.
대상 prod 데이터 아직 소규모라 CONCURRENTLY 불요. downgrade에서 drop.
참조: 전체검토 2026-06-29 성능 갈래(#1).
"""
from alembic import op

revision = "0126"
down_revision = "0125"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_voucher_lines_account", "voucher_lines",
                    ["account_code", "voucher_id"], schema="finance")


def downgrade() -> None:
    op.drop_index("ix_voucher_lines_account", table_name="voucher_lines", schema="finance")
