"""계약 전자서명 미러 필드 (e-sign #6) — HIS sign 결과 미러.

ERP는 서명 사용/미러 측(HIS=서명 SoT). 계약 2종(hr.medical_contracts·scm.outsourcing_contracts)에
HIS 봉투 상태·인증서·완료본 참조 미러 컬럼 추가. HIS→ERP `sign.completed` 인박스가 갱신.
모두 nullable — 기존 행/미서명 계약 무영향.

참조: docs/05-reference/erp-reply-his-open-requests-2026-06-28.md §A.
"""
import sqlalchemy as sa
from alembic import op

revision = "0124"
down_revision = "0123"
branch_labels = None
depends_on = None

_COLS = [
    ("sign_envelope_id", sa.String(64)),    # HIS multi-sign 봉투 id
    ("sign_status", sa.String(16)),         # NONE|REQUESTED|IN_PROGRESS|COMPLETED|REJECTED
    ("signed_at", sa.DateTime(timezone=True)),
    ("sign_cert_serial", sa.String(80)),    # 서명 인증서 일련
    ("sign_assurance", sa.String(20)),      # assuranceLevel(HSM_ANCHORED 등)
    ("sign_doc_ref", sa.String(300)),       # 완료본 회수 참조(URL/해시)
]


def upgrade() -> None:
    for tbl, sch in (("medical_contracts", "hr"), ("outsourcing_contracts", "scm")):
        for name, typ in _COLS:
            op.add_column(tbl, sa.Column(name, typ, nullable=True), schema=sch)


def downgrade() -> None:
    for tbl, sch in (("medical_contracts", "hr"), ("outsourcing_contracts", "scm")):
        for name, _ in _COLS:
            op.drop_column(tbl, name, schema=sch)
