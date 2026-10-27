"""0061 — HIS 인박스 이벤트 재처리(retry) + DEAD 상태.

FAILED 인박스 이벤트가 영구 정체하던 문제(P0-9) 해소 — 일시실패는 재시도 카운터·
다음 재시도 시각으로 자동 재처리, 한도 초과·핸들러 미구현은 DEAD로 분리(가시화).

Revision ID: 0061
"""
from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None

# prod 실제 제약명(PG 자동명) — 모델 name(ck_his_inbound_status)과 분기. 둘 다 IF EXISTS 처리.
_CHECK = "his_inbound_events_status_check"


def upgrade() -> None:
    op.execute("ALTER TABLE integration.his_inbound_events "
               "ADD COLUMN IF NOT EXISTS retry_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE integration.his_inbound_events "
               "ADD COLUMN IF NOT EXISTS next_retry_at TIMESTAMPTZ")
    op.execute(f"ALTER TABLE integration.his_inbound_events DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute("ALTER TABLE integration.his_inbound_events "
               "DROP CONSTRAINT IF EXISTS ck_his_inbound_status")
    op.execute(f"ALTER TABLE integration.his_inbound_events ADD CONSTRAINT {_CHECK} "
               "CHECK (status IN ('NEW','APPLIED','FAILED','DEAD'))")


def downgrade() -> None:
    op.execute(f"ALTER TABLE integration.his_inbound_events DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(f"ALTER TABLE integration.his_inbound_events ADD CONSTRAINT {_CHECK} "
               "CHECK (status IN ('NEW','APPLIED','FAILED'))")
    op.execute("ALTER TABLE integration.his_inbound_events DROP COLUMN IF EXISTS next_retry_at")
    op.execute("ALTER TABLE integration.his_inbound_events DROP COLUMN IF EXISTS retry_count")
