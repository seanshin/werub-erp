"""0187 — ITAM 조달 PO 연계.

입고된 구매발주(EQUIPMENT) 라인을 IT 자산으로 등록 시 출처 추적 — it_assets.procurement_po_line_id.
(scm.po_lines.id 참조·FK는 스키마 경계상 생략, 값 링크만)

Revision ID: 0187
"""
import sqlalchemy as sa
from alembic import op

revision = "0187"
down_revision = "0186"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("it_assets", sa.Column("procurement_po_line_id", sa.BigInteger),
                  schema="itam")
    op.create_index("ix_it_assets_po_line", "it_assets", ["procurement_po_line_id"],
                    schema="itam", postgresql_where=sa.text("procurement_po_line_id IS NOT NULL"))


def downgrade() -> None:
    op.drop_index("ix_it_assets_po_line", table_name="it_assets", schema="itam")
    op.drop_column("it_assets", "procurement_po_line_id", schema="itam")
