"""의료기관 회계기준 준비금 계정 — 운영성과표·기본금변동 (회계 도메인 심화).

비영리 의료법인 의료기관 회계기준 규칙의 자본/준비금: 고유목적사업준비금·의료발전준비금.
(기본금=기존 3100 자본금). 운영성과표는 코드 분류로 산출(statements.operating_statement).
ON CONFLICT DO NOTHING(멱등, 0087/0073 패턴). 준비금 전입/환입 전표는 회계사 정책 확정 후 후속.
"""
from alembic import op

revision = "0130"
down_revision = "0129"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO finance.accounts (code, name, kind, active) VALUES
            ('3210','고유목적사업준비금','CAPITAL', true),
            ('3220','의료발전준비금','CAPITAL', true)
        ON CONFLICT (code) DO NOTHING""")


def downgrade() -> None:
    pass   # 계정 삭제는 과거 집계 정합 위험 — 비활성만(0073 패턴)
