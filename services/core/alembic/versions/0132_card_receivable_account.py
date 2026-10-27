"""카드미수금 계정(1132) 분리 — 일반 미수금(1130)과 충돌 해소 (원무·정합).

기존엔 일마감 카드수납과 일반 미수금(settlement)이 같은 GL 1130을 공유 → aging·카드사 입금대사
구분 불가(전체검토 정합결함). 카드미수를 1132로 분리. 과거 1130 혼재분은 보존(컷오버 이후 분리).
ON CONFLICT DO NOTHING(멱등).
"""
from alembic import op

revision = "0132"
down_revision = "0131"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "INSERT INTO finance.accounts (code, name, kind, active) "
        "VALUES ('1132', '카드미수금', 'ASSET', true) ON CONFLICT (code) DO NOTHING")


def downgrade() -> None:
    pass
