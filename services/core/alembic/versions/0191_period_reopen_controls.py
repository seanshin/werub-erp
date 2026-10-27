"""0191 — 마감/재개 내부통제(G2): 기간 재개 추적 + 확정 스냅샷 버전보존.

기존: 재개(reopen)가 fiscal_periods.closed_by/closed_at를 NULL로 파괴하고, 재마감 시
closing_snapshots를 무버전으로 덮어써 확정 재무제표 이력이 소실 → 마감통제 감사 취약(G2).

보완:
- fiscal_periods: reopened_by/at/note + reopen_count 추가(마감 귀속 보존 + 재개 이력).
- closing_snapshots: version + superseded_at 추가. (yyyymm,kind) 유일제약을 "활성(미대체)만
  유일"인 부분 유니크 인덱스로 교체 → 재개 시 supersede, 재마감 시 새 버전 append.

Revision ID: 0191
"""
import sqlalchemy as sa
from alembic import op

revision = "0191"
down_revision = "0190"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fiscal_periods", sa.Column("reopened_by", sa.BigInteger(), nullable=True),
                  schema="finance")
    op.add_column("fiscal_periods", sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
                  schema="finance")
    op.add_column("fiscal_periods", sa.Column("reopen_note", sa.String(200), nullable=True),
                  schema="finance")
    op.add_column("fiscal_periods",
                  sa.Column("reopen_count", sa.Integer(), server_default="0", nullable=False),
                  schema="finance")

    op.add_column("closing_snapshots",
                  sa.Column("version", sa.Integer(), server_default="1", nullable=False),
                  schema="finance")
    op.add_column("closing_snapshots",
                  sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
                  schema="finance")
    op.drop_constraint("uq_closing_snapshot", "closing_snapshots", schema="finance", type_="unique")
    op.create_index("uq_closing_snapshot_active", "closing_snapshots", ["yyyymm", "kind"],
                    unique=True, schema="finance",
                    postgresql_where=sa.text("superseded_at IS NULL"))


def downgrade() -> None:
    op.drop_index("uq_closing_snapshot_active", table_name="closing_snapshots", schema="finance")
    op.create_unique_constraint("uq_closing_snapshot", "closing_snapshots", ["yyyymm", "kind"],
                                schema="finance")
    op.drop_column("closing_snapshots", "superseded_at", schema="finance")
    op.drop_column("closing_snapshots", "version", schema="finance")
    op.drop_column("fiscal_periods", "reopen_count", schema="finance")
    op.drop_column("fiscal_periods", "reopen_note", schema="finance")
    op.drop_column("fiscal_periods", "reopened_at", schema="finance")
    op.drop_column("fiscal_periods", "reopened_by", schema="finance")
