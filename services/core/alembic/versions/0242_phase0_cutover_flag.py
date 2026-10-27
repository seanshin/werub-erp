"""0242 — Phase0 컷오버 런타임 플래그 (관리화면 무재시작 토글).

common.org_settings.phase0_cutover_enabled — env HIS_PSEUDONYM_ENABLED와 OR로 게이트 판정.
관리화면에서 ADMIN이 합의 일시에 토글(무재시작)·즉시 롤백.

Revision ID: 0242
"""
import sqlalchemy as sa
from alembic import op

revision = "0242"
down_revision = "0241"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("org_settings",
                  sa.Column("phase0_cutover_enabled", sa.Boolean(), nullable=False,
                            server_default=sa.false()),
                  schema="common")


def downgrade() -> None:
    op.drop_column("org_settings", "phase0_cutover_enabled", schema="common")
