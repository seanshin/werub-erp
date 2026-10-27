"""0031 — 퇴직연금 유형(DB/DC): hr.employees.pension_type.

DB형=퇴직급여충당부채(내부적립), DC형=부담금 비용처리(외부적립·미지급금). 기본 DB.

Revision ID: 0031
"""
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE hr.employees ADD COLUMN pension_type VARCHAR(4) NOT NULL DEFAULT 'DB'")
    op.execute("ALTER TABLE hr.employees ADD CONSTRAINT ck_pension_type "
               "CHECK (pension_type IN ('DB','DC'))")


def downgrade() -> None:
    op.execute("ALTER TABLE hr.employees DROP CONSTRAINT ck_pension_type")
    op.execute("ALTER TABLE hr.employees DROP COLUMN pension_type")
