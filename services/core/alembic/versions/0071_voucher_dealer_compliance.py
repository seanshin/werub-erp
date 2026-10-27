"""0071 — 검진권 딜러 컴플라이언스·계층 필드(scm.suppliers).

HIS vendor.synced/changed가 보내나 0070에서 누락된 필드 보존: licenseNumber(국내
외국인환자 유치업 등록번호=법정요건)·contractRef(해외 제휴계약)·parentId(딜러 계층).
ADD COLUMN IF NOT EXISTS로 idempotent.

Revision ID: 0071
"""
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None

_COLS = [
    ("license_number", "VARCHAR(40)"),
    ("contract_ref", "VARCHAR(60)"),
    ("parent_external_code", "VARCHAR(40)"),
]


def upgrade() -> None:
    for name, ddl in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers ADD COLUMN IF NOT EXISTS {name} {ddl}")


def downgrade() -> None:
    for name, _ in _COLS:
        op.execute(f"ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS {name}")
