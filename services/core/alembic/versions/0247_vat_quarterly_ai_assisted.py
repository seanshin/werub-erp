"""0247 — 부가가치세 신고를 AI_ASSISTED로 전환(AI 보조 2호).

결정 대기함 `rectask-ai-assisted-scope`의 권고 2순위: 1호(주간 미수금·마이그 0246) 검증 후
`tax.vat_quarterly`로 확장. 매출·매입세액 집계는 이미 ERP 안(TaxService.vat_summary)이고
핸들러는 **부작용 없는 초안 집계까지만** — 공통매입세액 안분·공제율·신고서식은 세무사 확정
영역이며, 승인해도 신고·납부는 실행되지 않는다(HR 법정신고 3종 제외 원칙과 같은 이유로
금액 확정을 AI에 넘기지 않는다).

전환 내용: `execution_mode` MANUAL → AI_ASSISTED · `handler_ref` 지정.
행이 없으면(시드 전) 아무것도 하지 않는다 — 시드가 나중에 들어와도 코드 기본값이 맞다.

Revision ID: 0247
Revises: 0246
"""
from alembic import op

revision = "0247"
down_revision = "0246"
branch_labels = None
depends_on = None

_CODE = "tax.vat_quarterly"


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
