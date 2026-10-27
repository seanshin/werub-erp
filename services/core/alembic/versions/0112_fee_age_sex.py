"""0112 — 성·연령 적합성 자동화(P1 G5): fee_master 성/연령 제약 + 자동 AGE_SEX_LIMIT 룰.

fee_master.sex/min_age/max_age로 수가·약가의 성별·연령 적용제한을 마스터에 보관 →
AGE_SEX_LIMIT 룰(코드 미지정=자동)이 전 코드 일괄 점검(코드별 1룰 불필요).

Revision ID: 0112
"""
import sqlalchemy as sa
from alembic import op

revision = "0112"
down_revision = "0111"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("fee_master", sa.Column("sex", sa.String(length=1), nullable=True),
                  schema="regulatory")          # 'M'|'F' 적용 제한(없으면 무관)
    op.add_column("fee_master", sa.Column("min_age", sa.Integer(), nullable=True),
                  schema="regulatory")
    op.add_column("fee_master", sa.Column("max_age", sa.Integer(), nullable=True),
                  schema="regulatory")
    # 자동 성·연령 룰(코드 미지정=fee_master 기반 전 코드 점검). 초기 WARN(캘리브레이션 후 BLOCK 승격).
    op.execute(
        "INSERT INTO claims.pre_review_rules (code, name, rule_type, params, severity, active) "
        "VALUES ('PR-AGESEX-AUTO', '연령·성별 적합성(자동)', 'AGE_SEX_LIMIT', NULL, 'WARN', true) "
        "ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    op.execute("DELETE FROM claims.pre_review_rules WHERE code = 'PR-AGESEX-AUTO'")
    op.drop_column("fee_master", "max_age", schema="regulatory")
    op.drop_column("fee_master", "min_age", schema="regulatory")
    op.drop_column("fee_master", "sex", schema="regulatory")
