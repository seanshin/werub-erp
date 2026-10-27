"""0216 — 거래처별 AP aging: payables.party_ref(거래처 마스터 안정 연계키).

audit 발견: Payable.party가 자유문자열·거래처(vendor) 마스터 미연계 → 거래처별 채무 잔액/지급예정
관리 약함·party×버킷 매트릭스 부재. party_ref(예 'supplier:123')로 그룹핑 보강(동명·개명 안전).
finance는 scm import 불가라 FK 아닌 문자열 참조(voucher_ref 관례). 기존 행은 NULL→이름(party) 폴백.

Revision ID: 0216
"""
import sqlalchemy as sa
from alembic import op

revision = "0216"
down_revision = "0215"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("payables", sa.Column("party_ref", sa.String(40), nullable=True),
                  schema="finance")
    op.create_index("ix_payable_party_ref", "payables", ["party_ref"], schema="finance")


def downgrade() -> None:
    op.drop_index("ix_payable_party_ref", "payables", schema="finance")
    op.drop_column("payables", "party_ref", schema="finance")
