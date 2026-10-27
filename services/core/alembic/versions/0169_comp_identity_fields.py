"""0169 — 급여 기초정보 민감필드(HIS 인입) — 주민번호·은행코드·예금주 (연동 R-1/R-2).

hr.compensation_profiles에 resident_id_enc(주민번호 AES-GCM)·bank_code(표준 은행코드)·
account_holder(예금주) 추가. HIS 직원 셀프서비스 급여 기초정보 인입 수신용. nullable(무백필).

Revision ID: 0169
"""
import sqlalchemy as sa
from alembic import op

revision = "0169"
down_revision = "0168"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("compensation_profiles", sa.Column("bank_code", sa.String(4), nullable=True),
                  schema="hr")
    op.add_column("compensation_profiles", sa.Column("resident_id_enc", sa.LargeBinary(), nullable=True),
                  schema="hr")
    op.add_column("compensation_profiles", sa.Column("account_holder", sa.String(40), nullable=True),
                  schema="hr")


def downgrade() -> None:
    op.drop_column("compensation_profiles", "account_holder", schema="hr")
    op.drop_column("compensation_profiles", "resident_id_enc", schema="hr")
    op.drop_column("compensation_profiles", "bank_code", schema="hr")
