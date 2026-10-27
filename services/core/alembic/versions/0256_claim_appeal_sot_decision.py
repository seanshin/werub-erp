"""0256 — 결정 대기 1건: 이의신청 결과의 회계 원천(HIS 재심사 vs ERP 이의신청) (2026-10-09 시나리오 5회차).

revision 없는 HIS 재심사가 삭감을 줄이면 ERP 이의신청 인정 복원과 겹치는지 ERP가 판정할 수 없다(HIS 청구↔통보서
조정건 연결 없음 #621). 날짜·금액으로 추측하던 가드가 오탐·이중 복원을 오가 폐기하고, 운영자 확인(관리자 > 연동
인박스 '확인 후 반영')으로 보류한다. 근본 해소는 어느 쪽을 원천으로 할지의 결정 — 스키마 변경 없음(시드만).

Revision ID: 0256
Revises: 0255
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0256"
down_revision = "0255"
branch_labels = None
depends_on = None

_REF = "docs/01-plan/erp-scenario-integration-test-2026-10-09.plan.md"
SEEDS = [
    ("claim-appeal-outcome-sot", "INTEGRATION", "이의신청 결과의 회계 원천(HIS 재심사 vs ERP 이의신청)",
     "ERP는 심평원 결과통보(MEDRLT)의 조정건에 대한 이의신청을 기록하고, 인정되면 삭감손실을 복원하는 전표"
     "(보험미수금 1140 차변/삭감손실 5910 대변)를 만든다. HIS도 같은 결과를 반영한 재심사(claim.reviewed)를 다시 "
     "보낼 수 있는데, 두 경로는 서로 연결돼 있지 않아(HIS 청구↔통보서 조정건 연결 없음) ERP는 HIS 재심사의 삭감 "
     "감소가 이의 인정으로 이미 복원된 것인지 알 수 없다. 현재는 revision 없이 삭감이 줄어든 HIS 재심사를 자동 "
     "반영하지 않고 보류하며, 심사팀이 이의 결과와 대조해 관리자 > 연동 인박스에서 '확인 후 반영' 또는 "
     "'처리 완료(사유)'로 처리한다. 어느 쪽을 회계 원천으로 삼을지 정해야 보류를 없앨 수 있다.",
     [{"key": "HIS_REVIEW_SOT", "label": "HIS 재심사를 원천",
       "description": "ERP 이의신청은 기록만 하고 복원 전표를 만들지 않는다 — 복원은 HIS 재심사로만 반영"},
      {"key": "ERP_APPEAL_SOT", "label": "ERP 이의신청을 원천",
       "description": "HIS는 이의 결과 재심사를 보내지 않거나, 보낼 때 revision과 appealRef를 동봉해 ERP가 "
                      "이미 반영된 복원을 구분하게 한다(HIS 협의 필요)", "recommended": True},
      {"key": "KEEP_CONFIRM", "label": "현행 유지(운영자 확인)",
       "description": "삭감이 줄어든 revision 없는 재심사는 보류 — 심사팀이 건별로 대조해 확인 후 반영/처리 완료"}]),
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
