"""환자미수금 계정(1131) — 환자 본인부담 발생주의 전환 (원무·수납 심화).

환자 본인부담을 진료시점 DR 1131/CR 4110로 발생, 수납시점 DR 현금·카드/CR 1131로 상계
(보험분 1140과 일관). 계정 추가만 — 발생주의 전기는 config patient_accrual_enabled 게이트.
ON CONFLICT DO NOTHING(멱등·기존 정의 보존, 0087/0073 패턴).
"""
from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('1131', '환자미수금', 'ASSET', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    pass   # 계정 삭제는 과거 집계 정합 위험 — 비활성만(0073 패턴)
