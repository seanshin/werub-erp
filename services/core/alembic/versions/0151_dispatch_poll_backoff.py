"""AI 발신 큐 상태폴링 백오프 컬럼 — poll_count·next_poll_at.

2026-07-03 HIS 진단: 서버 "completed" 응답을 종결로 인정하지 않아 50건이 22일간 30초마다
재폴링(전체 트래픽 48%). 종결 동의어 수용 + 지수 백오프 + 72h 컷오프의 백오프 상태 저장용.
"""
import sqlalchemy as sa
from alembic import op

revision = "0151"
down_revision = "0150"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_dispatch_queue",
                  sa.Column("poll_count", sa.Integer, nullable=False, server_default="0"),
                  schema="regulatory")
    op.add_column("ai_dispatch_queue",
                  sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
                  schema="regulatory")


def downgrade() -> None:
    op.drop_column("ai_dispatch_queue", "next_poll_at", schema="regulatory")
    op.drop_column("ai_dispatch_queue", "poll_count", schema="regulatory")
