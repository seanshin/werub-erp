"""0254 — 위수탁 계약 status 컬럼 폭 12→16 (2026-10-09 시나리오 S-D) + 시나리오 결정 대기 1건.

0138이 CHECK 제약에 PENDING_APPROVAL(16자)을 넣으면서 컬럼(VARCHAR(12))은 넓히지 않았다(의료계약 0140은
넓혔다). 결재선 규칙에 걸린 계약은 상신·승인이 `StringDataRightTruncationError`로 500 — DRAFT에서 영영
발효되지 못한다. sqlite 테스트는 VARCHAR 길이를 강제하지 않아 놓쳤다. 모델(String(16))과 DB 폭 전수 대조 결과 어긋난 컬럼은 이것 하나.

결정 대기: 원무 화면(환자·접수·예약)을 접수 직원(STAFF)에게 열지 — 메뉴는 보였지만 서버 권한이 막아 빈 화면이었고,
메뉴를 서버 권한에 맞춰 줄였다(환자 PII라 권한을 넓히는 쪽은 사람 판단).

Revision ID: 0254
Revises: 0253
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0254"
down_revision = "0253"
branch_labels = None
depends_on = None

_REF = "docs/01-plan/erp-scenario-integration-test-2026-10-09.plan.md"
SEEDS = [
    ("frontdesk-staff-patient-screens", "POLICY", "접수 직원(STAFF)의 원무 화면(환자·접수·예약) 사용",
     "환자·접수·예약 화면은 메뉴상 전 직원에게 보였지만 서버 권한(patient.patients read)은 원장·관리자·부서장"
     "(자기 부서)만 허용해 직원에겐 빈 화면이었다. 메뉴를 서버 권한에 맞춰 줄였다. 원무 접수 직원이 이 화면을 "
     "써야 한다면 환자 개인정보 접근 권한을 넓히는 정책 결정이 필요하다.",
     [{"key": "KEEP_RESTRICTED", "label": "현행 유지(직원 비노출)",
       "description": "원무 업무는 HIS에서 수행·ERP 화면은 관리자/부서장용", "recommended": True},
      {"key": "GRANT_FRONTDESK", "label": "원무 부서 직원에게 부서 범위 조회 허용",
       "description": "patient.patients read를 원무 부서 STAFF에 DEPT 범위로 — 접속기록 감사 대상"}]),
]


def upgrade() -> None:
    op.alter_column("outsourcing_contracts", "status", schema="scm",
                    type_=sa.String(16), existing_type=sa.String(12), existing_nullable=False)
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
    # PENDING_APPROVAL 행이 있으면 좁힐 수 없다 — 그 계약을 먼저 처리(승인·반려)해야 한다
    op.alter_column("outsourcing_contracts", "status", schema="scm",
                    type_=sa.String(12), existing_type=sa.String(16), existing_nullable=False)
