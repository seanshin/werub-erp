"""0178 — ITAM R3: 자산 실사 시각(last_audited_at).

QR 현장 실사(F-9)가 위치만 갱신하고 실사 시각을 남기지 않아 전수 실사 입증·미실사 리포트가
불가하던 갭. it_assets.last_audited_at 추가.

Revision ID: 0178
"""
import sqlalchemy as sa
from alembic import op

revision = "0178"
down_revision = "0177"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("it_assets", sa.Column("last_audited_at", sa.DateTime(timezone=True)),
                  schema="itam")


def downgrade() -> None:
    op.drop_column("it_assets", "last_audited_at", schema="itam")
