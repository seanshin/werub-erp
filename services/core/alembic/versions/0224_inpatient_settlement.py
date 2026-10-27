"""0224 — 입원 정산 이벤트 3종 소비(HIS 요청 2026-07-23): 4115 입원수익 + 중간정산/퇴원정산 대사.

HIS billing.interim_recorded/interim_paid/discharge_settlement 인바운드 소비 기반.
- finance.accounts 4115 입원수익(REVENUE) — 외래(4110)와 분리(입원 기간 단위 계통).
- patient.interim_billings — 중간정산 원장(interim_billing_id 멱등 upsert·GENERATED→PAID).
- patient.inpatient_settlements — 퇴원 최종 정산 대사 스냅샷(admission_id upsert·값-멱등 재발행 대체).

Revision ID: 0224
"""
import sqlalchemy as sa
from alembic import op

revision = "0224"
down_revision = "0223"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind) VALUES
            ('4115','입원수익','REVENUE')
        ON CONFLICT (code) DO NOTHING
    """)
    op.create_table(
        "interim_billings",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("interim_billing_id", sa.String(64), nullable=False),
        sa.Column("admission_id", sa.String(64), nullable=True, index=True),
        sa.Column("chart_no", sa.String(20), nullable=False, index=True),
        sa.Column("sequence_no", sa.Integer(), nullable=True),
        sa.Column("period_start", sa.Date(), nullable=True),
        sa.Column("period_end", sa.Date(), nullable=True),
        sa.Column("total_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("insurance_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("copay_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("paid_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(12), nullable=False, server_default="GENERATED"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("interim_billing_id", name="uq_interim_billing_id"),
        schema="patient",
    )
    op.create_table(
        "inpatient_settlements",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("admission_id", sa.String(64), nullable=False),
        sa.Column("chart_no", sa.String(20), nullable=False, index=True),
        sa.Column("admit_date", sa.Date(), nullable=True),
        sa.Column("discharge_date", sa.Date(), nullable=True),
        sa.Column("days", sa.Integer(), nullable=True),
        sa.Column("total_charges", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("total_copay", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("already_paid", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("balance_due", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("balanced", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("overpaid", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("overpaid_amount", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("unpaid_interims", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("warnings", sa.JSON(), nullable=True),
        sa.Column("breakdown", sa.JSON(), nullable=True),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("admission_id", name="uq_inpatient_settlement_admission"),
        schema="patient",
    )


def downgrade() -> None:
    op.drop_table("inpatient_settlements", schema="patient")
    op.drop_table("interim_billings", schema="patient")
    op.execute("DELETE FROM finance.accounts WHERE code = '4115'")
