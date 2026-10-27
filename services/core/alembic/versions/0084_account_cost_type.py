"""0084 — 계정과목 cost_type(고정/변동): 대시보드 고정비 분류를 설정값으로 전환.

기존 하드코딩 고정비(급여 5310·퇴직급여 5320·감가상각 5210·사용권자산상각 5640·리스이자 5630)를
cost_type='FIXED'로 시드 → 경영진 대시보드 고정비 집계가 본 설정을 읽음(관리자가 화면에서 조정).

Revision ID: 0084
"""
from alembic import op
import sqlalchemy as sa

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("cost_type", sa.String(length=10), nullable=True),
                  schema="finance")
    # 기존 고정비 계정 시드(하드코딩과 동일) — 멱등
    op.execute("""
        UPDATE finance.accounts SET cost_type = 'FIXED'
        WHERE code IN ('5310','5320','5210','5640','5630') AND cost_type IS NULL
    """)
    # 변동비 대표(재료비) 시드
    op.execute("""
        UPDATE finance.accounts SET cost_type = 'VARIABLE'
        WHERE code = '5410' AND cost_type IS NULL
    """)


def downgrade() -> None:
    op.drop_column("accounts", "cost_type", schema="finance")
