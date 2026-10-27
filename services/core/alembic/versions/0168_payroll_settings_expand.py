"""0168 — 급여 정책 확장(상여합산 COMBINED·사업주부담 요율) + 복리후생비 계정.

payroll_settings에 combined_months·employer_ei_rate·employer_wc_rate 추가, 원천징수 방식 CHECK에
COMBINED 추가. finance.accounts에 5330 복리후생비(사업주 4대보험 부담금 전기 대상) 보강.

Revision ID: 0168
"""
from alembic import op
import sqlalchemy as sa

revision = "0168"
down_revision = "0167"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_settings",
                  sa.Column("combined_months", sa.Integer(), nullable=False, server_default="1"),
                  schema="hr")
    op.add_column("payroll_settings",
                  sa.Column("employer_ei_rate", sa.Numeric(6, 4), nullable=False,
                            server_default="0.0115"), schema="hr")
    op.add_column("payroll_settings",
                  sa.Column("employer_wc_rate", sa.Numeric(6, 4), nullable=False,
                            server_default="0.007"), schema="hr")
    op.drop_constraint("ck_payroll_wh_method", "payroll_settings", schema="hr")
    op.create_check_constraint(
        "ck_payroll_wh_method", "payroll_settings",
        "incentive_withholding_method IN ('NONE','FLAT','COMBINED')", schema="hr")
    # 사업주 4대보험 부담금 전기 대상 계정 — 재무제표 집계 정합(계정 마스터 등록 필수)
    op.execute("INSERT INTO finance.accounts (code, name, kind, active) "
               "VALUES ('5330', '복리후생비', 'EXPENSE', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.drop_constraint("ck_payroll_wh_method", "payroll_settings", schema="hr")
    op.create_check_constraint(
        "ck_payroll_wh_method", "payroll_settings",
        "incentive_withholding_method IN ('NONE','FLAT')", schema="hr")
    op.drop_column("payroll_settings", "employer_wc_rate", schema="hr")
    op.drop_column("payroll_settings", "employer_ei_rate", schema="hr")
    op.drop_column("payroll_settings", "combined_months", schema="hr")
