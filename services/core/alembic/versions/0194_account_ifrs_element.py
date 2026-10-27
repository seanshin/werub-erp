"""0194 — 계정 IFRS element 데이터화(G8 Phase0): accounts.is_current·cf_activity + 백필.

재무제표 분류가 statements.py에 하드코딩(prefix/set)돼 취약(G8). 계정마스터로 이관 시작.
Phase0=컬럼 추가 + 기존 하드코딩 규칙으로 백필(코드=데이터 미러). statements.py는 미변경
(재무제표 동작 불변·회귀 0). Phase1에서 골든 테스트 후 데이터 주도로 전환.

Revision ID: 0194
"""
import sqlalchemy as sa
from alembic import op

revision = "0194"
down_revision = "0193"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("accounts", sa.Column("is_current", sa.Boolean(), nullable=True), schema="finance")
    op.add_column("accounts", sa.Column("cf_activity", sa.String(10), nullable=True), schema="finance")

    # 백필(statements.py 규칙 미러) — 유동/비유동
    op.execute("UPDATE finance.accounts SET is_current = TRUE "
               "WHERE kind = 'ASSET' AND code LIKE '11%'")
    op.execute("UPDATE finance.accounts SET is_current = FALSE "
               "WHERE kind = 'ASSET' AND code NOT LIKE '11%'")
    op.execute("UPDATE finance.accounts SET is_current = TRUE "
               "WHERE kind = 'LIABILITY' AND (code LIKE '21%' OR code LIKE '23%') "
               "AND code NOT IN ('2120','2130','2510')")
    op.execute("UPDATE finance.accounts SET is_current = FALSE "
               "WHERE kind = 'LIABILITY' AND NOT ((code LIKE '21%' OR code LIKE '23%') "
               "AND code NOT IN ('2120','2130','2510'))")

    # 백필 — 현금흐름 활동(명시 매핑 세트만·나머지는 NULL→statements.py kind 폴백 유지)
    op.execute("UPDATE finance.accounts SET cf_activity = 'OPERATING' "
               "WHERE code IN ('1130','1140','1150','2110','1810','1815','1825','2120')")
    op.execute("UPDATE finance.accounts SET cf_activity = 'INVESTING' "
               "WHERE code IN ('1710','1720')")
    op.execute("UPDATE finance.accounts SET cf_activity = 'FINANCING' "
               "WHERE code IN ('2210','3300','3400')")


def downgrade() -> None:
    op.drop_column("accounts", "cf_activity", schema="finance")
    op.drop_column("accounts", "is_current", schema="finance")
