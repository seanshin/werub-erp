"""성능 인덱스 — 폴링·소비·급여·퇴직충당 풀스캔 제거 (P0 SQL 튜닝).

실서버 pg_stat_user_tables 실측 기반:
- ai_dispatch_queue: 워커 30초 폴링(claim_next/SENT)이 인덱스 없어 seq_scan 23만회.
- his_inbound_events: inbound 소비 루프 status 필터 미인덱싱(seq_scan ~3천).
- payroll_lines: 모델 index=True 선언됐으나 prod 미생성(드리프트) — idx_scan=0 항상 풀스캔.
- retirement_provisions: employee_id 필터 미인덱싱(seq_scan ~1.3천).

대상 테이블 소규모라 CONCURRENTLY 불요. downgrade에서 전부 drop.
참조: docs/03-analysis/performance-sql-tuning-review.md §2
"""
import sqlalchemy as sa
from alembic import op

revision = "0106"
down_revision = "0105"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) ai_dispatch_queue 폴링 핫패스: WHERE dispatch_status=? AND filter_status IN(...) ORDER BY urgency DESC, id
    op.create_index(
        "ix_ai_dispatch_claim", "ai_dispatch_queue",
        ["dispatch_status", "filter_status", sa.text("urgency DESC"), "id"],
        schema="regulatory")

    # 2) his_inbound_events 소비 루프: status='NEW' / (status='FAILED' AND next_retry_at<=now) ORDER BY id
    op.create_index(
        "ix_his_inbound_status_retry", "his_inbound_events",
        ["status", "next_retry_at", "id"],
        schema="integration")

    # 3) payroll_lines: 모델 index=True 정합(드리프트 해소) — record_id 조회/조인
    op.create_index(
        "ix_payroll_lines_record_id", "payroll_lines",
        ["record_id"], schema="hr")

    # 4) retirement_provisions: 직원별 누적 조회 WHERE employee_id=? AND period<? ORDER BY period DESC
    op.create_index(
        "ix_retire_emp_period", "retirement_provisions",
        ["employee_id", sa.text("period DESC")], schema="hr")


def downgrade() -> None:
    op.drop_index("ix_retire_emp_period", table_name="retirement_provisions", schema="hr")
    op.drop_index("ix_payroll_lines_record_id", table_name="payroll_lines", schema="hr")
    op.drop_index("ix_his_inbound_status_retry", table_name="his_inbound_events", schema="integration")
    op.drop_index("ix_ai_dispatch_claim", table_name="ai_dispatch_queue", schema="regulatory")
