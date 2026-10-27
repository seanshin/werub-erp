"""0167 — 급여 정책 설정(성과급 원천징수 방식) + 성과급 소득세 카탈로그(H1).

hr.payroll_settings(싱글턴·성과급 원천징수 NONE/FLAT·기본 NONE=현행) + pay_item_catalog INCTAX 행.
성과급 원천징수는 세무 정책이라 하드코딩 대신 관리화면(/admin/payroll-settings)에서 설정.

Revision ID: 0167
"""
import sqlalchemy as sa
from alembic import op

revision = "0167"
down_revision = "0166"
branch_labels = None
depends_on = None


def upgrade() -> None:
    t = op.create_table(
        "payroll_settings",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("incentive_withholding_method", sa.String(10), nullable=False,
                  server_default="NONE"),
        sa.Column("incentive_withholding_rate", sa.Numeric(6, 4), nullable=False,
                  server_default="0"),
        sa.Column("employer_burden_posting", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("id = 1", name="ck_payroll_settings_singleton"),
        sa.CheckConstraint("incentive_withholding_method IN ('NONE','FLAT')",
                           name="ck_payroll_wh_method"),
        schema="hr",
    )
    op.bulk_insert(t, [{"id": 1, "incentive_withholding_method": "NONE",
                        "incentive_withholding_rate": 0, "employer_burden_posting": False}])
    # 성과급 소득세(원천징수) 항목 — FLAT 방식 시 성과급 라인과 함께 생성(외부 관리 라인)
    op.execute("INSERT INTO hr.pay_item_catalog "
               "(code, name, kind, category, taxable, display_order, is_medical) "
               "VALUES ('INCTAX', '성과급 소득세', 'DEDUCTION', '세금', false, 62, true) "
               "ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM hr.pay_item_catalog WHERE code = 'INCTAX'")
    op.drop_table("payroll_settings", schema="hr")
