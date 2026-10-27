"""0037 — 수당 단가 마스터: hr.pay_rate_settings.

기존 payroll_rules 코드 상수(당직비·야간시급·자격수당·감시단속수당)를 DB로 —
단협·취업규칙 변경 시 코드 수정 없이 관리자 화면에서 조정.
미설정 키는 코드 기본값 유지(하위호환).

Revision ID: 0037
"""
from alembic import op

revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None

_SEED = [
    ("DUTY_PAY", 100000, "당직 1회 (의사·전공의)"),
    ("NIGHT_HOURLY", 12000, "야간·교대 시간당 (간호사)"),
    ("WATCH_ALLOWANCE", 100000, "감시단속(경비) 월 수당"),
    ("QUAL_PHARMACIST", 200000, "자격수당 — 약사"),
    ("QUAL_RADIOTECH", 150000, "자격수당 — 방사선사"),
    ("QUAL_LABTECH", 120000, "자격수당 — 임상병리사"),
    ("QUAL_PHYSIOTHERAPIST", 120000, "자격수당 — 물리치료사"),
    ("QUAL_TECH", 120000, "자격수당 — 의료기사(기타)"),
]


def upgrade() -> None:
    op.execute("""
        CREATE TABLE hr.pay_rate_settings (
            id BIGSERIAL PRIMARY KEY,
            rate_key VARCHAR(40) NOT NULL UNIQUE,
            amount NUMERIC(12, 0) NOT NULL,
            description VARCHAR(120),
            updated_by BIGINT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    for key, amount, desc in _SEED:
        op.execute("INSERT INTO hr.pay_rate_settings (rate_key, amount, description) "
                   f"VALUES ('{key}', {amount}, '{desc}')")


def downgrade() -> None:
    op.execute("DROP TABLE hr.pay_rate_settings")
