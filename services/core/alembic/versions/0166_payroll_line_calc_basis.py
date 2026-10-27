"""0166 — 급여 라인 계산근거(calc_basis) — 임금명세서 계산방법(P1·§27의2 5호).

hr.payroll_lines.calc_basis(JSON·표시 전용) — 연장/야간/공제 등 계산식(시간수×통상임금×가산율·
기준액×요율)을 명세서에 표기. nullable(과거 라인 무백필 → 계산식 행 생략).

Revision ID: 0166
"""
import sqlalchemy as sa
from alembic import op

revision = "0166"
down_revision = "0165"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payroll_lines", sa.Column("calc_basis", sa.JSON(), nullable=True),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("payroll_lines", "calc_basis", schema="hr")
