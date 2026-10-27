"""0171 — ITAM M2: 소프트웨어·디지털 서비스 마스터(4유형) + 좌석 배정.

itam.software_services(구매SW·SaaS·AI·클라우드 유형화)·itam.service_allocations(좌석 준수).

Revision ID: 0171
"""
import sqlalchemy as sa
from alembic import op

revision = "0171"
down_revision = "0170"
branch_labels = None
depends_on = None

_TYPES = "('PERPETUAL_SW','SAAS','AI_SUBSCRIPTION','CLOUD','OPEN_SOURCE')"
_BILLING = "('ONE_TIME','SEAT_RECURRING','USAGE_METERED')"


def upgrade() -> None:
    op.create_table(
        "software_services",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("vendor", sa.String(120)),
        sa.Column("edition", sa.String(80)),
        sa.Column("service_type", sa.String(16), nullable=False, server_default="SAAS"),
        sa.Column("billing_model", sa.String(16), nullable=False, server_default="SEAT_RECURRING"),
        sa.Column("billing_cycle", sa.String(10)),
        sa.Column("seats", sa.Integer),
        sa.Column("license_metric", sa.String(12)),
        sa.Column("alloc_basis", sa.String(8)),
        sa.Column("unit_cost", sa.Numeric(16, 2)),
        sa.Column("recurring_cost", sa.Numeric(16, 2)),
        sa.Column("currency", sa.String(3), nullable=False, server_default="KRW"),
        sa.Column("account_ref", sa.String(120)),
        sa.Column("portal_url", sa.String(200)),
        sa.Column("contract_id", sa.BigInteger, sa.ForeignKey("itam.vendor_contracts.id")),
        sa.Column("owner_employee_id", sa.BigInteger),
        sa.Column("cost_center", sa.String(40)),
        sa.Column("started_on", sa.Date),
        sa.Column("expires_on", sa.Date),
        sa.Column("auto_renew", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("cancel_notice_days", sa.Integer),
        sa.Column("license_key_enc", sa.LargeBinary),
        sa.Column("intangible_asset_id", sa.BigInteger),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(f"service_type IN {_TYPES}", name="ck_svc_type"),
        sa.CheckConstraint(f"billing_model IN {_BILLING}", name="ck_svc_billing"),
        schema="itam",
    )
    op.create_index("ix_software_services_type", "software_services", ["service_type"], schema="itam")
    op.create_index("ix_software_services_expires", "software_services", ["expires_on"], schema="itam")

    op.create_table(
        "service_allocations",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("service_id", sa.BigInteger, sa.ForeignKey("itam.software_services.id"),
                  nullable=False, index=True),
        sa.Column("it_asset_id", sa.BigInteger, sa.ForeignKey("itam.it_assets.id")),
        sa.Column("employee_id", sa.BigInteger, sa.ForeignKey("hr.employees.id")),
        sa.Column("allocated_on", sa.Date, nullable=False),
        sa.Column("released_on", sa.Date),
        sa.Column("note", sa.Text),
        schema="itam",
    )


def downgrade() -> None:
    op.drop_table("service_allocations", schema="itam")
    op.drop_table("software_services", schema="itam")
