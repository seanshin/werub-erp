"""마약류 수불 거래일(entry_on) — 월 수불일지 거래월 귀속 (FR-20/21).

기존엔 거래일 컬럼이 없어 monthly_ledger(법정 월별 수불일지)가 입력시각(created_at)으로
월을 필터 → 전월 거래를 익월에 입력하면 잘못된 월에 잡혔다. entry_on(거래일) 추가.

⚠️ narcotics_ledger는 append-only(해시체인 불변성)로 erp_app에 UPDATE가 REVOKE돼 있어
기존 행 백필이 불가하다. 따라서 컬럼은 **nullable**로 추가하고, monthly_ledger가
entry_on 없으면 created_at으로 폴백(legacy 행 하위호환). 신규 행은 add_entry가 항상 채움.

참조: 원무·재고·연동 집중검토 2026-06-27.
"""
import sqlalchemy as sa
from alembic import op

revision = "0122"
down_revision = "0121"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # nullable 추가만 — append-only(UPDATE revoke)라 백필 불가. 폴백은 monthly_ledger가 처리.
    op.add_column("narcotics_ledger", sa.Column("entry_on", sa.Date(), nullable=True),
                  schema="scm")


def downgrade() -> None:
    op.drop_column("narcotics_ledger", "entry_on", schema="scm")
