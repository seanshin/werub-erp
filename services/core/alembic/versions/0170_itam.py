"""0170 — ITAM(IT 자산관리) M1: itam 스키마 + 하드웨어 대장·배정·벤더 계약.

itam.vendor_contracts·itam.it_assets·itam.asset_assignments. 회계 감가상각은 finance가 담당하고
it_assets.finance_asset_id로 연결만 한다(중복 없음).

Revision ID: 0170
"""
import sqlalchemy as sa
from alembic import op

revision = "0170"
down_revision = "0169"
branch_labels = None
depends_on = None

_STATUSES = "('IN_STOCK','ASSIGNED','REPAIR','LOANER','LOST','STOLEN','RETIRED','DISPOSED')"


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS itam")

    op.create_table(
        "vendor_contracts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("vendor", sa.String(120), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False, server_default="PURCHASE"),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("start_on", sa.Date),
        sa.Column("end_on", sa.Date),
        sa.Column("amount", sa.Numeric(16, 0)),
        sa.Column("sla_note", sa.String(200)),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        schema="itam",
    )

    op.create_table(
        "it_assets",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_tag", sa.String(40), nullable=False, unique=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default="LAPTOP"),
        sa.Column("serial_no", sa.String(80), unique=True),
        sa.Column("model", sa.String(80)),
        sa.Column("manufacturer", sa.String(80)),
        sa.Column("status", sa.String(10), nullable=False, server_default="IN_STOCK"),
        sa.Column("owner_department_id", sa.BigInteger),
        sa.Column("assigned_employee_id", sa.BigInteger,
                  sa.ForeignKey("hr.employees.id")),
        sa.Column("location", sa.String(120)),
        sa.Column("acquired_on", sa.Date),
        sa.Column("warranty_until", sa.Date),
        sa.Column("ip_addr", sa.String(45)),
        sa.Column("hostname", sa.String(80)),
        sa.Column("mac_addr", sa.String(17)),
        sa.Column("handles_phi", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("security_grade", sa.String(8)),
        sa.Column("disk_encrypted", sa.Boolean),
        sa.Column("av_installed", sa.Boolean),
        sa.Column("udi", sa.String(60)),
        sa.Column("mfds_no", sa.String(40)),
        sa.Column("calibration_due", sa.Date),
        sa.Column("spec", sa.JSON),
        sa.Column("finance_asset_id", sa.BigInteger),
        sa.Column("contract_id", sa.BigInteger,
                  sa.ForeignKey("itam.vendor_contracts.id")),
        sa.Column("note", sa.Text),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint(f"status IN {_STATUSES}", name="ck_itasset_status"),
        schema="itam",
    )
    op.create_index("ix_it_assets_status", "it_assets", ["status"], schema="itam")
    op.create_index("ix_it_assets_dept", "it_assets", ["owner_department_id"], schema="itam")

    op.create_table(
        "asset_assignments",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.BigInteger, sa.ForeignKey("itam.it_assets.id"),
                  nullable=False, index=True),
        sa.Column("employee_id", sa.BigInteger, sa.ForeignKey("hr.employees.id"), nullable=False),
        sa.Column("assigned_on", sa.Date, nullable=False),
        sa.Column("returned_on", sa.Date),
        sa.Column("assigned_by", sa.BigInteger),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("return_condition", sa.String(120)),
        sa.Column("note", sa.Text),
        schema="itam",
    )


def downgrade() -> None:
    op.drop_table("asset_assignments", schema="itam")
    op.drop_table("it_assets", schema="itam")
    op.drop_table("vendor_contracts", schema="itam")
    op.execute("DROP SCHEMA IF EXISTS itam")
