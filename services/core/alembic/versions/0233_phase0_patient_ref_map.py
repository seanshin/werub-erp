"""0233 — Phase0 가명 토큰 대사표 (patientRef ↔ chartNo).

HIS `pseudonymizeChartNo` 컷오버 대비 — 1회성 대사표 이관(873명)·미등록 보류 큐.
integration.patient_ref_map(1:1 대사)·integration.pending_patient_ref(신규환자 보류).

Revision ID: 0233
"""
import sqlalchemy as sa
from alembic import op

revision = "0233"
down_revision = "0232"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patient_ref_map",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("chart_no", sa.String(20), nullable=False),
        sa.Column("patient_ref", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False, server_default="HIS_MAP"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("chart_no", name="uq_patient_ref_map_chart"),
        sa.UniqueConstraint("patient_ref", name="uq_patient_ref_map_ref"),
        schema="integration",
    )
    op.create_table(
        "pending_patient_ref",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("patient_ref", sa.String(32), nullable=False),
        sa.Column("first_event", sa.String(64), nullable=True),
        sa.Column("event_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("resolved_chart_no", sa.String(20), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("first_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("patient_ref", name="uq_pending_patient_ref"),
        schema="integration",
    )


def downgrade() -> None:
    op.drop_table("pending_patient_ref", schema="integration")
    op.drop_table("patient_ref_map", schema="integration")
