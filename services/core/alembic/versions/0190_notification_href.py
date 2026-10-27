"""0190 — 알림에 딥링크(href) 추가(X2-3).

알림(Notification)이 제목·본문 텍스트만 있어 클릭해도 해당 엔티티/화면으로 이동 못 하던 것을
href(프론트 라우트)로 딥링크. 기한 임박·비용 점검 등 알림에서 바로 대상 화면으로 이동.

Revision ID: 0190
"""
import sqlalchemy as sa
from alembic import op

revision = "0190"
down_revision = "0189"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("notifications", sa.Column("href", sa.String(200), nullable=True),
                  schema="common")


def downgrade() -> None:
    op.drop_column("notifications", "href", schema="common")
