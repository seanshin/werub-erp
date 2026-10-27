"""0257 — 결정 대기 `statutory-2026-rates-check` 갱신: 고시 대조 완료 → 남은 결정은 '지급분 소급 정산' (2026-10-10).

0250이 올린 '고시 대조 필요'는 대조가 끝났다 — 코드의 2026 요율은 2025 복사본이었고(복지부 보도자료·공단 기준소득월액
상·하한으로 확인), v1.291.0에서 코드 상수를 고시값으로 정정했다. 이제 남은 사람의 결정은 **이미 낮은 요율로 확정·지급된
2026 급여의 차액을 어떻게 할지**다(차액은 관리 화면 '요율 차액 미리보기'로 직원별 확인). PENDING일 때만 고친다
(DECIDED 결정문은 근거라 불변). 스키마 변경 없음.

Revision ID: 0257
Revises: 0256
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0257"
down_revision = "0256"
branch_labels = None
depends_on = None

_KEY = "statutory-2026-rates-check"
_TITLE = "2026 4대보험 요율 정정 완료 — 낮은 요율로 지급된 2026 급여 차액 처리"
_Q = (
    "고시 대조 결과 코드의 2026 근로자 요율은 2025 값 복사본이었습니다(국민연금 4.5%→고시 4.75% · 건강보험 3.545%→3.595% · "
    "장기요양 건보료의 12.95%→13.14% · 연금 기준소득월액 2026.1~6월 40만/637만원·7월분부터 41만/659만원). 근거: 보건복지부 "
    "보도자료 '2026년 건강보험료율 7.19%로 결정'·'2026년도 장기요양보험료율 0.9448%', 개정 국민연금법(2026 9.5%), 공단 "
    "기준소득월액 상·하한 안내. v1.291.0에서 코드 상수를 정정해 **이후 생성하는 급여부터** 고시 요율이 적용됩니다.\n"
    "남은 결정: v1.291.0 이전 판으로 2026 급여를 확정·지급했다면 2025 요율이 적용돼 있습니다 — 근로자 공제가 적게 됐고, "
    "사업주 부담(같은 금액)도 적게 전기됐습니다. 직원별·항목별 차액은 관리자 > 급여 설정 '요율 차액 미리보기'에서 "
    "확인하세요. 미확정 급여는 재생성하면 새 요율이 적용됩니다. 먼저 ERP 급여가 실제 지급의 원천인지"
    "(외부 급여 시스템·공단 고지와의 관계)를 확인한 뒤 처리 방법을 고르세요.")
_OPTS = [
    {"key": "NEXT_PAYROLL_ADJUST", "label": "다음 급여에서 차액 정산",
     "description": "다음 지급 급여에서 직원별 차액(근로자분)을 추가 공제하고 사업주분 부담금을 보정 전기 — 직원 안내 필요",
     "recommended": True},
    {"key": "EMPLOYER_ABSORB", "label": "근로자분 차액을 기관이 부담",
     "description": "직원 추가 공제 없이 차액을 기관 비용으로 처리(사업주 부담분 보정 전기는 동일)"},
    {"key": "NOT_PAYROLL_SOT", "label": "ERP 급여는 지급 원천이 아님",
     "description": "실제 급여·공단 신고는 외부에서 처리 — ERP 기록은 참고용이라 정산 불필요(사유 기록)"},
]

# 0250 원문 — downgrade가 제목·질문·선택지·참조를 그대로 되돌린다(새 질문이 옛 제목 아래 남지 않게)
_OLD_TITLE = "2026년 4대보험 요율이 2025년 값과 동일 — 고시 대조 필요"
_OLD_Q = ("코드에 등록된 2026 근로자 요율(국민연금 4.5%·건강보험 3.545%·장기요양 12.95%·연금 하한 "
          "390,000원)이 2025년과 완전히 같습니다. 2026년에는 개정 국민연금법에 따른 보험료율 인상과 "
          "건강보험료율 인상이 있었던 것으로 알려져 있습니다 — 검토 시 **후보값**(원문 미확인): 국민연금 "
          "4.75%(9.5%의 절반)·건강보험 3.595%(7.19%)·장기요양 약 13.14%·연금 하한 400,000원(2025-07 고시). "
          "먼저 prod에서 ERP가 2026 급여를 실제로 계산했는지(급여 레코드 건수) 확인하세요. 고시를 대조해 맞으면 '현행 확인', 다르면 관리화면 /admin/payroll-settings "
          "'법정 요율'에 2026 값을 등록하세요(이후 생성분부터 적용 — 이미 지급한 달의 차액 정산은 공단 정산"
          "·급여 소급 절차로 별도 처리).")
_OLD_OPTS = [
    {"key": "REGISTER", "label": "고시값 등록(차이 있음)",
     "description": "관리화면에 2026 고시 요율 등록 — 이후 급여부터 반영·소급 차액 별도 정산",
     "recommended": True},
    {"key": "CONFIRMED", "label": "현행 확인(차이 없음)",
     "description": "고시 대조 결과 코드 값이 맞음 — 근거를 결정 메모에 기록"}]


def upgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE common.pending_decisions SET title = :t, question = :q, options = :o, "
        "context_ref = :r WHERE key = :k AND status = 'PENDING'"),
        {"k": _KEY, "t": _TITLE, "q": _Q, "o": json.dumps(_OPTS, ensure_ascii=False),
         "r": "docs/01-plan/erp-statutory-2026-rates-fix-2026-10-10.plan.md"})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "UPDATE common.pending_decisions SET title = :t, question = :q, options = :o, "
        "context_ref = :r WHERE key = :k AND status = 'PENDING'"),
        {"k": _KEY, "t": _OLD_TITLE, "q": _OLD_Q, "o": json.dumps(_OLD_OPTS, ensure_ascii=False),
         "r": "services/core/src/erp/hr/statutory.py RATES"})
