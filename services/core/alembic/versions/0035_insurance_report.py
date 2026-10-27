"""0035 — 4대보험 취득·상실 신고 추적: hr.insurance_report_events.

입사(취득)·퇴사(상실)·휴직(납부예외)·복직(재개) 이벤트를 자동 적재,
신고 기한(due_on)·처리 상태(PENDING/REPORTED)를 추적 — 신고 누락 방지.
계산·서식 집계는 기존 statutory/tax 모듈, 본 테이블은 '이벤트·기한' 관리.

Revision ID: 0035
"""
from alembic import op

revision = "0035"
down_revision = "0034"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.insurance_report_events (
            id BIGSERIAL PRIMARY KEY,
            employee_id BIGINT NOT NULL REFERENCES hr.employees(id),
            event VARCHAR(8) NOT NULL CHECK (event IN ('ACQUIRE','LOSE','EXEMPT','RESUME')),
            basis_on DATE NOT NULL,
            due_on DATE NOT NULL,
            status VARCHAR(10) NOT NULL DEFAULT 'PENDING'
                CHECK (status IN ('PENDING','REPORTED')),
            reported_on DATE,
            reported_by BIGINT,
            note TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_ins_report_event UNIQUE (employee_id, event, basis_on)
        )
    """)
    op.execute("CREATE INDEX ix_ins_report_status ON hr.insurance_report_events (status)")


def downgrade() -> None:
    op.execute("DROP TABLE hr.insurance_report_events")
