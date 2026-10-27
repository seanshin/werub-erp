"""0015 — hr.leave_requests 전자결재 연동 컬럼 (approval_id·view_url).

HIS W.Sign 릴레이 연동 — 상신 시 approval_id/view_url 저장, 콜백으로 status 반영.

Revision ID: 0015
"""
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.leave_requests ADD COLUMN approval_id VARCHAR(64)")
    op.execute("ALTER TABLE hr.leave_requests ADD COLUMN view_url VARCHAR(300)")
    op.execute("CREATE INDEX ix_leave_approval ON hr.leave_requests (approval_id) "
               "WHERE approval_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS hr.ix_leave_approval")
    op.execute("ALTER TABLE hr.leave_requests DROP COLUMN approval_id")
    op.execute("ALTER TABLE hr.leave_requests DROP COLUMN view_url")
