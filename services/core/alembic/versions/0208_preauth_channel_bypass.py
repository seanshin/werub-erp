"""0208 — HIS e결재 회계연계 보완: 사전승인 채널·응급우회·사후승인 상태 컬럼.

HIS 문서 재점검(누락 3건 보완) 반영:
- preauth.order_approved에 PORTAL 유형 명시 → `channel`(STANDARD·PORTAL·EMERGENCY_BYPASS).
- 응급 POST_FACTO 우회(사전승인 없이 발행된 마약류·수혈·고가검사) 인지 → `order_type`(임상 유형·규제/감사 축)
  + `post_approved`(응급우회=False 사후승인 미결·집계 노출).

Revision ID: 0208
"""
import sqlalchemy as sa
from alembic import op

revision = "0208"
down_revision = "0207"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("preauth_orders",
                  sa.Column("channel", sa.String(20), nullable=False, server_default="STANDARD"),
                  schema="finance")
    op.add_column("preauth_orders",
                  sa.Column("order_type", sa.String(40), nullable=True), schema="finance")
    op.add_column("preauth_orders",
                  sa.Column("post_approved", sa.Boolean(), nullable=False,
                            server_default=sa.true()),
                  schema="finance")


def downgrade() -> None:
    op.drop_column("preauth_orders", "post_approved", schema="finance")
    op.drop_column("preauth_orders", "order_type", schema="finance")
    op.drop_column("preauth_orders", "channel", schema="finance")
