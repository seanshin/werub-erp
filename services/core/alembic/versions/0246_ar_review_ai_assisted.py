"""0246 — 주간 미수금 점검을 AI_ASSISTED로 전환(AI 보조 1호).

결정 대기함 `rectask-ai-assisted-scope`의 권고안: AI 보조는 `ops.ar_review_weekly` 1건부터.
근거 ① 데이터가 전부 ERP 안(연령분석·독촉이력) ② **법정 신고가 아니라 틀려도 사람이 확정 전에
잡는다** ③ 주간이라 피드백 루프가 빠르다. HR 법정신고 3종(4대보험·원천세·연말정산)은 대상에서
의도적으로 제외 — 금액은 이미 결정론적 계산 엔진이 산출하고, AI가 끼면 책임 소재만 흐려진다.

전환 내용: `execution_mode` MANUAL → AI_ASSISTED · `handler_ref` 지정.
핸들러는 **부작용이 없다**(읽기만·초안 생성). 승인은 사람이 `/admin/decisions`에서 한다.
행이 없으면(시드 전) 아무것도 하지 않는다 — 시드가 나중에 들어와도 코드 기본값이 맞다.

Revision ID: 0246
Revises: 0245
"""
from alembic import op

revision = "0246"
down_revision = "0245"
branch_labels = None
depends_on = None

_CODE = "ops.ar_review_weekly"


def upgrade() -> None:
    op.execute(f"""
        UPDATE common.recurring_tasks
           SET execution_mode = 'AI_ASSISTED', handler_ref = '{_CODE}'
         WHERE code = '{_CODE}' AND execution_mode = 'MANUAL'
    """)


def downgrade() -> None:
    op.execute(f"""
        UPDATE common.recurring_tasks
           SET execution_mode = 'MANUAL', handler_ref = NULL
         WHERE code = '{_CODE}' AND execution_mode = 'AI_ASSISTED'
    """)
