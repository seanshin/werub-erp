"""0255 — HIS 인박스 RESOLVED 상태 + 운영자 종결 기록 (2026-10-09 시나리오 3회차).

재심사 추론 정정 가드(이의 복원 이중반영·진료월 미상·낡은 재생)와 정정 충돌이 보낸 DEAD는 재큐해도 같은
가드에 다시 걸리는데, 메시지는 '종결하라'고 하면서 종결 수단이 없었다(출구 없는 DEAD). 관리자가 사유를 남기고
종결하는 RESOLVED 상태(DEAD에서만)와 누가·언제·왜를 남기는 컬럼을 추가한다. error는 보존한다.

Revision ID: 0255
Revises: 0254
"""
from alembic import op

revision = "0255"
down_revision = "0254"
branch_labels = None
depends_on = None

_T = "integration.his_inbound_events"
# 0061이 PG 자동명으로 재생성했다(모델 name ck_his_inbound_status와 분기) — 둘 다 IF EXISTS로 정리.
_CHECK = "his_inbound_events_status_check"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_T} ADD COLUMN IF NOT EXISTS resolved_by BIGINT")
    op.execute(f"ALTER TABLE {_T} ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
    op.execute(f"ALTER TABLE {_T} ADD COLUMN IF NOT EXISTS resolve_reason VARCHAR(300)")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT IF EXISTS ck_his_inbound_status")
    op.execute(f"ALTER TABLE {_T} ADD CONSTRAINT {_CHECK} "
               "CHECK (status IN ('NEW','APPLIED','FAILED','DEAD','RESOLVED'))")


def downgrade() -> None:
    # RESOLVED는 DEAD로 되돌린다(종결 전 상태) — 제약을 좁히기 전에 데이터부터.
    op.execute(f"UPDATE {_T} SET status = 'DEAD' WHERE status = 'RESOLVED'")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT IF EXISTS {_CHECK}")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT IF EXISTS ck_his_inbound_status")
    op.execute(f"ALTER TABLE {_T} ADD CONSTRAINT {_CHECK} "
               "CHECK (status IN ('NEW','APPLIED','FAILED','DEAD'))")
    op.execute(f"ALTER TABLE {_T} DROP COLUMN IF EXISTS resolve_reason")
    op.execute(f"ALTER TABLE {_T} DROP COLUMN IF EXISTS resolved_at")
    op.execute(f"ALTER TABLE {_T} DROP COLUMN IF EXISTS resolved_by")
