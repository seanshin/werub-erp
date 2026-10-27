"""0117 — HIS 신규 동봉 필드 소비(PR #75 회신): billing_lines.

ingredient_code(주성분·DUR 직접 매칭)·pregnancy_status(임부금기 DUR)·
institution_grade(종별·HIS config)·price_basis(BASE_CATALOG — 종별가산 SoT=ERP).

Revision ID: 0117
"""
import sqlalchemy as sa
from alembic import op

revision = "0117"
down_revision = "0116"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for col in ("ingredient_code", "pregnancy_status", "institution_grade", "price_basis"):
        op.add_column("billing_lines", sa.Column(col, sa.String(length=20), nullable=True),
                      schema="patient")


def downgrade() -> None:
    for col in ("price_basis", "institution_grade", "pregnancy_status", "ingredient_code"):
        op.drop_column("billing_lines", col, schema="patient")
