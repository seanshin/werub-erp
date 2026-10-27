"""0201 — 야간수당 산정 방식 설정(M-11): hr.payroll_settings.night_pay_mode + 시드.

야간수당이 정액단가(12,000/h) 하드 방식뿐이라 통상시급 높은 간호사의 법정 야간가산(§56③) 미달
가능성이 정책 결정 없이 방치되던 것을 설정으로: FLAT(정액·현행) | STATUTORY(통상시급×0.5) |
MAX(둘 중 큰 값). 기본·시드 = FLAT(무회귀 — 기관이 관리화면에서 전환).

Revision ID: 0201
"""
import sqlalchemy as sa
from alembic import op

revision = "0201"
down_revision = "0200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_settings",
                  sa.Column("night_pay_mode", sa.String(10), nullable=False,
                            server_default="FLAT"),
                  schema="hr")
    op.create_check_constraint("ck_payroll_night_mode", "payroll_settings",
                               "night_pay_mode IN ('FLAT','STATUTORY','MAX')", schema="hr")
    # 시드 — 설정 행이 없으면 싱글턴 행 생성(전 컬럼 server_default = 현행 정책·night=FLAT·M-11)
    op.execute("INSERT INTO hr.payroll_settings (id) VALUES (1) ON CONFLICT (id) DO NOTHING")


def downgrade() -> None:
    op.drop_constraint("ck_payroll_night_mode", "payroll_settings", schema="hr")
    op.drop_column("payroll_settings", "night_pay_mode", schema="hr")
