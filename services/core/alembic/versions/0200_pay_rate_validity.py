"""0200 — 수당 단가 기간화(거버넌스): hr.pay_rate_settings.valid_from + (rate_key, valid_from) 유니크.

단가가 현재값 단일 행이라 (a) 개정 시 과거 급여 재생성이 새 단가로 소급 왜곡, (b) 연 단위 단협
갱신 이력이 없던 것 해소. 기존 행은 valid_from='2020-01-01'(전 기간 유효)로 백필 — 무회귀.

⚠ 원 UNIQUE는 0037 인라인 생성이라 PG 자동명(pay_rate_settings_rate_key_key) — 모델명과 둘 다
DROP IF EXISTS(제약명 분기 교훈·2026-06-15).

Revision ID: 0200
"""
import sqlalchemy as sa
from alembic import op

revision = "0200"
down_revision = "0199"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("pay_rate_settings",
                  sa.Column("valid_from", sa.Date(), nullable=False,
                            server_default="2020-01-01"),
                  schema="hr")
    op.execute("ALTER TABLE hr.pay_rate_settings "
               "DROP CONSTRAINT IF EXISTS pay_rate_settings_rate_key_key")
    op.execute("ALTER TABLE hr.pay_rate_settings "
               "DROP CONSTRAINT IF EXISTS uq_pay_rate_settings_rate_key")
    op.create_unique_constraint("uq_pay_rate_period", "pay_rate_settings",
                                ["rate_key", "valid_from"], schema="hr")
    op.create_index("ix_hr_pay_rate_settings_rate_key", "pay_rate_settings", ["rate_key"],
                    schema="hr")


def downgrade() -> None:
    op.drop_index("ix_hr_pay_rate_settings_rate_key", table_name="pay_rate_settings", schema="hr")
    op.drop_constraint("uq_pay_rate_period", "pay_rate_settings", schema="hr")
    # 기간 이력이 있으면 rate_key 유니크 복원이 불가할 수 있음 — 최신 행만 남기고 정리 후 복원
    op.execute("""
        DELETE FROM hr.pay_rate_settings a USING hr.pay_rate_settings b
        WHERE a.rate_key = b.rate_key AND a.valid_from < b.valid_from
    """)
    op.create_unique_constraint("pay_rate_settings_rate_key_key", "pay_rate_settings",
                                ["rate_key"], schema="hr")
    op.drop_column("pay_rate_settings", "valid_from", schema="hr")
