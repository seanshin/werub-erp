"""0205 — 성능 인덱스(적대적 성능검토): 무한성장·풀스캔 결합 테이블 우선.

정적 성능검토 발견:
- audit.access_log / audit.audit_log: 인덱스 전무(PK만)·법정 2년 append-only 무한성장인데 목록조회가
  ORDER BY at DESC 풀스캔+정렬, 리텐션 대량삭제도 WHERE at<cutoff 풀스캔.
- patient.billing_lines.treated_on: HIS 진료행위 라인 미러(대형)·간호등급/미수aging/사전심사/리텐션이
  treated_on 기간범위를 chart_no prune 없이 스캔.
- scm.stock_movements: 모델 item_id index=True 선언이나 raw-SQL CREATE TABLE(0004)이 인덱스 미생성
  → prod 실존 없음(드리프트·payroll_lines 0106 동형). COGS 범위집계가 movement_type/moved_on 스캔.
- finance.receivables (source_module, source_ref): 0144가 payables만 인덱싱·receivables 누락(비대칭)
  → 전기마다 멱등조회가 풀스캔.

⚠ prod 소규모(수백~천행)라 CREATE INDEX 즉시. **실서비스(대량) 배포 시 access_log/audit_log/billing_lines는
CONCURRENTLY 권장**(트랜잭션 밖·잠금 회피) — production-deploy-backlog 반영.

Revision ID: 0205
"""
from alembic import op

revision = "0205"
down_revision = "0204"
branch_labels = None
depends_on = None

_INDEXES = [
    # (name, table, schema, columns-SQL)
    ("ix_access_log_at", "access_log", "audit", "at DESC"),
    ("ix_access_log_actor_at", "access_log", "audit", "actor, at DESC"),
    ("ix_audit_log_at", "audit_log", "audit", "at DESC"),
    ("ix_audit_log_entity_target", "audit_log", "audit", "entity, target_id, id DESC"),
    ("ix_billing_lines_treated_on", "billing_lines", "patient", "treated_on"),
    ("ix_stock_movements_item", "stock_movements", "scm", "item_id"),
    ("ix_stock_movements_type_on", "stock_movements", "scm", "movement_type, moved_on"),
    ("ix_receivable_source", "receivables", "finance", "source_module, source_ref"),
]


def upgrade() -> None:
    for name, table, schema, cols in _INDEXES:
        op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {schema}.{table} ({cols})")


def downgrade() -> None:
    for name, table, schema, _cols in _INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {schema}.{name}")
