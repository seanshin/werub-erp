"""환자안전사고 보고 (환자안전법 §14 자율보고) — regulatory.patient_safety_incidents (규제 심화).

근접오류·위해사건·적신호사건 접수·검토·종결·재발방지 추적. 법정의무인데 미구현이던 영역(전체검토).
"""
from alembic import op
import sqlalchemy as sa

revision = "0131"
down_revision = "0130"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "patient_safety_incidents",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("occurred_on", sa.Date(), nullable=False),
        sa.Column("incident_type", sa.String(12), nullable=False),
        sa.Column("severity", sa.String(10), nullable=False, server_default="NONE"),
        sa.Column("category", sa.String(40)),
        sa.Column("department_id", sa.BigInteger()),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("reporter_id", sa.BigInteger()),
        sa.Column("status", sa.String(10), nullable=False, server_default="REPORTED"),
        sa.Column("action", sa.Text()),
        sa.Column("reviewed_by", sa.BigInteger()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("incident_type IN ('NEAR_MISS','ADVERSE','SENTINEL')", name="ck_psi_type"),
        sa.CheckConstraint("severity IN ('NONE','MILD','MODERATE','SEVERE','DEATH')",
                           name="ck_psi_severity"),
        sa.CheckConstraint("status IN ('REPORTED','REVIEWING','ACTIONED','CLOSED')",
                           name="ck_psi_status"),
        schema="regulatory",
    )
    op.create_index("ix_psi_occurred", "patient_safety_incidents", ["occurred_on"],
                    schema="regulatory")


def downgrade() -> None:
    op.drop_index("ix_psi_occurred", table_name="patient_safety_incidents", schema="regulatory")
    op.drop_table("patient_safety_incidents", schema="regulatory")
