"""0230 — 복리후생 (Tier3): 제도 마스터·연 부여·신청/승인/지급.

복지포인트·경조사비·자기계발비·건강검진 지원 등 복리후생 제도를 직원별 연 한도로 관리.
신청→승인→지급(잔액 차감·한도 검증)·지급 시 GL(DR 5330 복리후생비 / CR 1110 현금).

- benefit_programs: 제도 마스터(유형·연 한도·단위·대상).
- benefit_grants: 직원×연도 부여(한도/포인트 적립).
- benefit_claims: 신청→승인→지급(금액·사유·상태·전표).

Revision ID: 0230
"""
import sqlalchemy as sa
from alembic import op

revision = "0230"
down_revision = "0229"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "benefit_programs",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("code", sa.String(30), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("benefit_type", sa.String(12), nullable=False, server_default="POINT"),
        sa.Column("unit", sa.String(8), nullable=False, server_default="KRW"),
        sa.Column("annual_limit", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("target", sa.String(16), nullable=False, server_default="ALL"),
        sa.Column("target_value", sa.String(40), nullable=True),
        sa.Column("requires_receipt", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("code", name="uq_benefit_program_code"),
        sa.CheckConstraint(
            "benefit_type IN ('POINT','SUBSIDY','CONGRATS','EVENT','OTHER')",
            name="ck_bprogram_type"),
        sa.CheckConstraint("unit IN ('KRW','POINT')", name="ck_bprogram_unit"),
        sa.CheckConstraint("target IN ('ALL','JOB_CATEGORY','DEPT')", name="ck_bprogram_target"),
        schema="hr",
    )
    op.create_table(
        "benefit_grants",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("program_id", sa.BigInteger(), sa.ForeignKey("hr.benefit_programs.id"),
                  nullable=False, index=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("granted", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("note", sa.String(200), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("program_id", "employee_id", "year", name="uq_benefit_grant"),
        schema="hr",
    )
    op.create_table(
        "benefit_claims",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("program_id", sa.BigInteger(), sa.ForeignKey("hr.benefit_programs.id"),
                  nullable=False, index=True),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("hr.employees.id"),
                  nullable=False, index=True),
        sa.Column("year", sa.Integer(), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("claim_on", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("status", sa.String(10), nullable=False, server_default="REQUESTED", index=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("paid_on", sa.Date(), nullable=True),
        sa.Column("voucher_ref", sa.String(60), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_bclaim_amount"),
        sa.CheckConstraint("status IN ('REQUESTED','APPROVED','PAID','REJECTED')",
                           name="ck_bclaim_status"),
        schema="hr",
    )


def downgrade() -> None:
    op.drop_table("benefit_claims", schema="hr")
    op.drop_table("benefit_grants", schema="hr")
    op.drop_table("benefit_programs", schema="hr")
