"""전체검토 Tier-3 성능 — 결재 워크리스트·미지급 멱등조회 인덱스.

approval_instances PENDING 스캔(pending_for) + payables (source_module, source_ref) 멱등조회
(월마감 계약 accrue가 계약수만큼 풀스캔하던 것 제거).
"""
from alembic import op
import sqlalchemy as sa

revision = "0144"
down_revision = "0143"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 워크리스트는 status='PENDING' 전건을 스캔 — 부분 인덱스로 PENDING만 조회(만료분 제외).
    op.create_index("ix_appinst_status_pending", "approval_instances", ["status"],
                    schema="common", postgresql_where=sa.text("status='PENDING'"))
    # 미지급 멱등 조회 (source_module, source_ref) — 월마감 계약 accrue가 매 계약 풀스캔하던 것 제거.
    op.create_index("ix_ap_source", "payables", ["source_module", "source_ref"], schema="finance")


def downgrade() -> None:
    op.drop_index("ix_ap_source", table_name="payables", schema="finance")
    op.drop_index("ix_appinst_status_pending", table_name="approval_instances", schema="common")
