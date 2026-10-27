"""일마감 동시성 — daily_closings 부분 유니크(일자당 CLOSED 1건) (FR-18/54).

close_day가 _is_closed 확인→insert 사이(TOCTOU)에 동시 요청이 둘 다 통과하면 같은 일자에
CLOSED 2건·전표 이중 계상 가능. DB 부분 유니크로 최종 방어(일자당 CLOSED 1건). amend는
원 마감을 AMENDED로 내린 뒤 CLOSED를 넣으므로 위반 없음.

참조: 원무·재고·연동 집중검토 2026-06-27.
"""
import sqlalchemy as sa
from alembic import op

revision = "0123"
down_revision = "0122"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("uq_closing_active", "daily_closings", ["closing_on"], unique=True,
                    schema="patient", postgresql_where=sa.text("status = 'CLOSED'"))


def downgrade() -> None:
    op.drop_index("uq_closing_active", table_name="daily_closings", schema="patient")
