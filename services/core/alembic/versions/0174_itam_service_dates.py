"""0174 — ITAM: SW·서비스 등록일자·개시일자·유료 첫 결제일·결제방식.

software_services에 registered_on(등록일)·first_billing_on(유료 첫 결제일)·payment_terms
(PREPAID/POSTPAID/FREE_TRIAL) 추가. started_on(개시일)은 기존 컬럼 재사용.

Revision ID: 0174
"""
import sqlalchemy as sa
from alembic import op

revision = "0174"
down_revision = "0173"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("software_services", sa.Column("registered_on", sa.Date), schema="itam")
    op.add_column("software_services", sa.Column("first_billing_on", sa.Date), schema="itam")
    op.add_column("software_services", sa.Column("payment_terms", sa.String(10)), schema="itam")


def downgrade() -> None:
    op.drop_column("software_services", "payment_terms", schema="itam")
    op.drop_column("software_services", "first_billing_on", schema="itam")
    op.drop_column("software_services", "registered_on", schema="itam")
