"""0221 — 성능: 열린 채권/채무 부분 인덱스 (성능 감사 HIGH).

audit 발견: receivables/payables가 status 선두 인덱스 없어 aging·cash_forecast·aging_by_party·
overdue_list가 매 호출 전체 heap 스캔(대다수 행이 종국상태 COLLECTED/WRITTEN_OFF/PAID인데도).
부분 인덱스로 open 집합만 색인 → 5개 사용자 대면 경로 커버·색인 크기 최소.

Revision ID: 0221
"""
from alembic import op

revision = "0221"
down_revision = "0220"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 환자 미수 회수(overdue_list)·aging AR·cash_forecast·aging_by_party AR 커버
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ar_open ON finance.receivables (source_module, due_on)
        WHERE status NOT IN ('COLLECTED','WRITTEN_OFF')
    """)
    # aging AP·cash_forecast·aging_by_party AP 커버
    op.execute("""
        CREATE INDEX IF NOT EXISTS ix_ap_open ON finance.payables (due_on)
        WHERE status <> 'PAID'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS finance.ix_ap_open")
    op.execute("DROP INDEX IF EXISTS finance.ix_ar_open")
