"""0078 — 딜러 세무·송금 마스터 컬럼암호화(평문→AES-GCM).

보안 검토(HIGH): tax_id·계좌·SWIFT는 HR 보수 프로필과 동급 민감정보인데 평문 저장.
HR 표준(design §6.4 AES-GCM 컬럼암호화)과 일치시킴. 평문 컬럼 제거→_enc(bytes) 추가.
prod 검증: tax_id/bank 채워진 supplier 0건이라 데이터 손실 없음(아직 실 딜러 미동기화).
은행명(bank_name)은 비민감이라 평문 유지.

Revision ID: 0078
"""
from alembic import op
import sqlalchemy as sa

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None

_PLAIN = ["tax_id", "bank_account", "bank_swift"]
_ENC = ["tax_id_enc", "bank_account_enc", "bank_swift_enc"]


def upgrade() -> None:
    for c in _ENC:
        op.add_column("suppliers", sa.Column(c, sa.LargeBinary), schema="scm")
    for c in _PLAIN:
        op.execute(f"ALTER TABLE scm.suppliers DROP COLUMN IF EXISTS {c}")


def downgrade() -> None:
    for c, t in zip(_PLAIN, [40, 40, 16]):
        op.add_column("suppliers", sa.Column(c, sa.String(t)), schema="scm")
    for c in _ENC:
        op.drop_column("suppliers", c, schema="scm")
