"""0077 — 정산 회신 아웃박스 조회 인덱스(status, next_retry_at).

송신 job이 매 2분 QUEUED/재시도도래 FAILED를 status·next_retry_at로 스캔 → 아웃박스
증가 시 풀스캔 방지(품질 검토 보완).

Revision ID: 0077
"""
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_settle_cb_status", "dealer_settlement_callbacks",
                    ["status", "next_retry_at"], schema="finance")


def downgrade() -> None:
    op.drop_index("ix_settle_cb_status", table_name="dealer_settlement_callbacks", schema="finance")
