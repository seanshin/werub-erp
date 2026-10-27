"""0252 — 전체 흐름 검토(2026-10-09) 연동 보완: 심사 정정 버전 마커 + HIS 계약 확인 결정 4건.

- claims.claim_submissions.decided_rev: claim.reviewed 정정(revision)을 확정액에 반영했는지 — 종전엔 최초
  리뷰만 반영해 정정 승인액 입금이 1140 대신 2141로 오분리됐다.
- 결정 대기함: ERP만으로 정할 수 없는 HIS 이벤트 계약 4건(일마감·취소 금액·재심사 revision·장비 불용).

Revision ID: 0252
Revises: 0251
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0252"
down_revision = "0251"
branch_labels = None
depends_on = None

_REF = "docs/01-plan/erp-full-flow-review-2026-10-09.plan.md"
SEEDS = [
    ("his-billing-settled-refund-netting", "INTEGRATION",
     "HIS 일마감 집계와 당일 환불·취소의 이중 차감",
     "HIS 일마감(billing.settled)은 PAID/PARTIAL 원장만 집계해, 마감 전 전액환불(REFUNDED)·부분수납 취소"
     "(CANCELLED)분은 일마감에서 빠진다. 그런데 billing.adjusted가 같은 금액을 따로 역분개해 수익·현금이 "
     "환불액만큼 이중으로 줄어든다. 어느 쪽에서 반영할지 HIS와 정해야 한다.",
     [{"key": "SETTLED_GROSS", "label": "일마감은 총수납(환불 전) 집계",
       "description": "HIS가 상태 필터 없이 그날 수납 총액을 보내고 환불은 adjusted로만 — ERP 무변경",
       "recommended": True},
      {"key": "SETTLED_NET", "label": "일마감은 순수납, 당일 환불은 adjusted 미발신",
       "description": "HIS가 당일 환불을 일마감에 반영하고 adjusted를 보내지 않음"}]),
    ("his-billing-cancel-amount", "INTEGRATION", "HIS 미수납 청구 취소 이벤트의 금액",
     "HIS 취소 이벤트는 amount=paidAmount ?? totalAmount라, 수납하지 않은(PENDING) 청구를 취소해도 청구액이 "
     "온다. ERP는 실수납 여부를 알 수 없어 받은 적 없는 금액을 역분개한다.",
     [{"key": "PAID_OR_ZERO", "label": "HIS가 paidAmount ?? 0으로 수정",
       "description": "미수납 취소는 0 → ERP가 건너뜀", "recommended": True},
      {"key": "SEND_PAID_FLAG", "label": "HIS가 실수납액 필드를 별도 동봉", "description": "ERP가 그 필드로 판정"}]),
    ("his-claim-reviewed-revision", "INTEGRATION", "HIS 재심사·재제출 이벤트의 revision 동봉",
     "HIS가 재심사로 금액을 바꾸면 새 ref로 보내지만 revision이 없어 ERP가 최초본으로 보고 금액 상이로 DEAD "
     "처리한다. 재제출(claim.submitted)도 같은 키로 무시된다. 또 transmitted=false(심평원 미전송) 청구도 "
     "보험미수금으로 인식된다.",
     [{"key": "SEND_REVISION", "label": "HIS가 revision·correctionOf 동봉, transmitted 의미 확정",
       "description": "ERP는 정정 경로로 처리·미전송 청구는 보류", "recommended": True},
      {"key": "KEEP", "label": "현행", "description": "재심사는 DEAD로 남아 수동 처리"}]),
    ("his-equipment-out-of-service", "INTEGRATION", "HIS equipment.out_of_service 이벤트 처리",
     "HIS는 장비 사용중지를 equipment.out_of_service로 보내지만 ERP에 핸들러가 없어 즉시 DEAD가 된다. "
     "사용중지가 회계상 처분인지(장부 제거) 일시 중지인지 정해야 한다.",
     [{"key": "STATUS_ONLY", "label": "일시 중지 — 상태만 기록",
       "description": "ERP는 수신 확인·상태 표시만(감가 계속)", "recommended": True},
      {"key": "DISPOSAL", "label": "처분 — 장부 제거",
       "description": "처분 전표(처분손익)까지 — 처분 사유 확인 필요"}]),
    ("retirement-income-tax-params-check", "GENERAL", "퇴직소득세 계산 파라미터 확인(세무)",
     "퇴직정산이 퇴직소득세를 원천징수하도록 바뀌었다(종전 미징수 — 소득세법 §146 위반). 근속연수공제표는 "
     "2023년 이후 퇴직분, 환산급여공제표는 2020년 이후 기준을 2026년까지 그대로 적용했다고 가정했다. "
     "비과세 퇴직소득·IRP 과세이연·세액공제는 반영하지 않았다. 세무 확인이 필요하다(hr/statutory.py "
     "retirement_income_tax). 미사용 연차수당은 상여 방식으로 원천징수하지만 연말정산 총급여 집계에 아직 빠진다.",
     [{"key": "CONFIRMED", "label": "세무 확인 — 현행 파라미터 맞음", "description": "근거를 결정 메모에", "recommended": True},
      {"key": "UPDATE", "label": "파라미터 갱신 필요", "description": "고시값으로 코드 갱신(개발)"}]),
    ("payroll-proration-rule", "GENERAL", "입사·퇴사월 기본급 일할 규칙(취업규칙)",
     "급여 일괄생성이 미래 퇴사 예정자를 조용히 빠뜨리고 월중 입사자에게 기본급 전액을 주던 결함을 고치며, "
     "입·퇴사월 기본급을 '재직 달력일수 ÷ 그 달 달력일수'로 일할하도록 했다(퇴사일=최종 근무일·양끝 포함). "
     "연장·야간 시급과 정액 수당은 일할하지 않는다. 기관 취업규칙과 맞는지 확인이 필요하다.",
     [{"key": "CALENDAR_DAYS", "label": "달력일수 일할(현행 구현)", "description": "확인만", "recommended": True},
      {"key": "WORKING_DAYS", "label": "소정근로일수 일할", "description": "개발 변경 필요"},
      {"key": "FULL_MONTH", "label": "일할하지 않음(전액)", "description": "취업규칙상 전액 지급인 경우"}]),
]


def upgrade() -> None:
    op.add_column("claim_submissions", sa.Column("decided_rev", sa.Integer(), nullable=True),
                  schema="claims")
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
    op.drop_column("claim_submissions", "decided_rev", schema="claims")
