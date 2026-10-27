"""scm.lots 유효기간 부분 인덱스 — 잔량 있는 로트만(성능검토 2026-07-04).

컴플라이언스 대시보드·재고 유효기간 조회가 `remaining_qty>0 AND expiry_on<=cutoff`로 필터하는데
expiry_on 무인덱스 → lots 전건 seqscan. Lot은 입고마다 생성·소진 후 잔존(무한 성장) → 부분 인덱스로
활성(잔량>0) 로트만 색인(잔량 소진분 제외로 인덱스 소형 유지).
"""
import sqlalchemy as sa
from alembic import op

revision = "0152"
down_revision = "0151"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_lots_expiry_active", "lots", ["expiry_on"], schema="scm",
                    postgresql_where=sa.text("remaining_qty > 0"))


def downgrade() -> None:
    op.drop_index("ix_lots_expiry_active", table_name="lots", schema="scm")
