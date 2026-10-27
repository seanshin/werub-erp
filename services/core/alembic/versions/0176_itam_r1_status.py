"""0176 — ITAM R1: 서비스 해지 상태 + PHI sticky.

software_services.status(ACTIVE/CANCELLED)·cancelled_on(F-2) + it_assets.phi_ever(S-2·소거 게이트
회피 방지·기존 handles_phi=true 백필).

Revision ID: 0176
"""
import sqlalchemy as sa
from alembic import op

revision = "0176"
down_revision = "0175"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("software_services",
                  sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
                  schema="itam")
    op.add_column("software_services", sa.Column("cancelled_on", sa.Date), schema="itam")
    op.add_column("it_assets",
                  sa.Column("phi_ever", sa.Boolean, nullable=False, server_default=sa.false()),
                  schema="itam")
    # 기존 PHI 취급 자산은 sticky 백필
    op.execute("UPDATE itam.it_assets SET phi_ever = true WHERE handles_phi = true")


def downgrade() -> None:
    op.drop_column("it_assets", "phi_ever", schema="itam")
    op.drop_column("software_services", "cancelled_on", schema="itam")
    op.drop_column("software_services", "status", schema="itam")
