"""0261 — 결정 대기 1건: 2026 귀속 연말정산을 어디서 할지(ERP 간이 vs 외부 vs ERP 확장) (2026-10-10 연말 대비).

ERP 연말정산은 간이 산식(근로소득공제·인적공제·자녀세액공제·근로소득세액공제)뿐이라 보험료·의료비·교육비·기부금·
카드·주택·연금계좌·표준세액공제가 빠져 결정세액이 실제보다 크게 나온다. 간소화자료 반입·공제신고서·지급명세서 파일·
환급/추가징수의 2월 급여 반영도 없다. 2027년 1~2월 정산 전에 범위를 정해야 한다. 스키마 변경 없음(시드만).

Revision ID: 0261
Revises: 0260
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0261"
down_revision = "0260"
branch_labels = None
depends_on = None

_REF = "docs/01-plan/erp-year-end-2027-readiness-2026-10-10.plan.md"
SEEDS = [
    ("year-end-settlement-scope", "HR", "2026 귀속 연말정산을 어디서 할지(ERP 간이 / 외부 / ERP 확장)",
     "ERP의 연말정산은 간이 산식입니다 — 근로소득공제·기본 인적공제·자녀세액공제·근로소득세액공제만 반영하고, "
     "보험료·의료비·교육비·기부금·신용카드·주택자금·연금계좌·표준세액공제는 없습니다(결정세액이 실제보다 큼). "
     "홈택스 간소화자료 반입, 직원 공제신고서, 근로소득 지급명세서 전자신고 파일, 환급·추가징수의 2월 급여 반영도 "
     "없습니다. 급여 원천징수(월)와 총급여·기납부세액 집계는 ERP에 있습니다(ESS 원천징수영수증 자료). "
     "2027년 1~2월 정산 전에 범위를 정해 주세요. 정기업무 '연말정산'(1/31)·'지급명세서 제출'(3/10)이 등록돼 있습니다.",
     [{"key": "EXTERNAL", "label": "외부(세무 프로그램·홈택스)에서 정산",
       "description": "ERP는 총급여·기납부세액 집계(원천징수영수증 자료)만 제공 — 정산 결과(환급/추가징수)는 "
                      "2월 급여에 수동 입력", "recommended": True},
      {"key": "ERP_SIMPLE", "label": "ERP 간이 정산 그대로",
       "description": "현 간이 산식 결과를 참고용으로만 사용(특별공제 미반영을 직원에게 안내)"},
      {"key": "ERP_EXTEND", "label": "ERP에 정식 연말정산 구축",
       "description": "공제신고서·간소화자료 반입·특별공제 전 항목·지급명세서 파일·2월 급여 반영 — 별도 기획(2027 정산엔 촉박)"}]),
]


def upgrade() -> None:
    conn = op.get_bind()
    for key, cat, title, q, opts in SEEDS:
        conn.execute(sa.text(
            "INSERT INTO common.pending_decisions (key, category, title, question, options, "
            "context_ref) VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"),
            {"k": key, "c": cat, "t": title, "q": q,
             "o": json.dumps(opts, ensure_ascii=False), "r": _REF})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM common.pending_decisions WHERE key = ANY(:ks) AND status = 'PENDING'"),
        {"ks": [x[0] for x in SEEDS]})
