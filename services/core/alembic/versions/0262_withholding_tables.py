"""0262 — 근로소득 간이세액표(별표 2) 테이블 + 2026.3.1 시행 후보 + 결정 대기 1건 (2026-10-10).

월 원천징수가 표가 아닌 근사 산식이었다. 표는 법정 데이터라
**후보(CANDIDATE)** 로만 넣는다 — 관리자 > 급여 설정 「근로소득 간이세액표」에서 홈택스 원표와 표본 칸을 대조해 출처를 적고 활성화한
것만 급여 계산에 쓴다. 후보 데이터는 별표 2(개정 2026.2.27) PDF 텍스트 추출본(src/erp/hr/data/withholding_table_2026-03-01.csv).

Revision ID: 0262
Revises: 0261
"""
import hashlib
import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "0262"
down_revision = "0261"
branch_labels = None
depends_on = None

_CSV = Path(__file__).resolve().parents[2] / "src" / "erp" / "hr" / "data" / "withholding_table_2026-03-01.csv"
_CHILD = {"one": 20830, "two": 45830, "extra": 33330}
_KEY = "withholding-2026-07-overwithheld"
_REF = "docs/01-plan/erp-withholding-table-2026-10-10.plan.md"
_Q = ("월 소득세 원천징수가 국세청 근로소득 간이세액표가 아니라 근사 산식(연금보험료공제·특별소득공제 추정 누락)으로 계산돼, "
      "v1.297.0 이전 판으로 확정한 급여는 표보다 더 떼였을 수 있습니다. "
      "관리자 > 급여 설정 「근로소득 간이세액표」에서 2026.3.1 시행 표를 홈택스 원표와 대조해 활성화하면 "
      "이후 생성하는 급여부터 표로 계산됩니다(같은 화면 '영향 미리보기'로 직원별 차액 확인). 이미 지급·신고된 달의 처리를 정하세요. "
      "먼저 ERP 급여가 실제 지급·원천징수 신고의 원천인지 확인하세요.")
_OPTS = [
    {"key": "YEAR_END", "label": "연말정산에서 정산",
     "description": "과다 원천징수분은 2026 귀속 연말정산(2027-02)에서 기납부세액으로 환급 — 별도 조치 없음(직원 안내)",
     "recommended": True},
    {"key": "REFUND_NEXT", "label": "다음 급여에서 조정",
     "description": "원천징수 과다분을 다음 달 원천징수에서 조정(세무 담당 확인 필요·원천징수이행상황신고 수정 여부)"},
    {"key": "NOT_PAYROLL_SOT", "label": "ERP 급여는 지급 원천 아님",
     "description": "실제 원천징수는 외부에서 처리 — ERP 기록은 참고용"},
]


def _rows() -> list[list[int]]:
    rows = []
    for line in _CSV.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and not line.startswith("lo_k"):
            rows.append([int(x) for x in line.split(",")])
    return rows


def upgrade() -> None:
    op.create_table(
        "withholding_tables",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("effective_from", sa.Date, nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="CANDIDATE"),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("source_note", sa.String(500)),
        sa.Column("rows", sa.JSON, nullable=False),
        sa.Column("child_deduction", sa.JSON, nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("activated_by", sa.BigInteger),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('CANDIDATE','ACTIVE','RETIRED')", name="ck_wht_status"),
        schema="hr",
    )
    op.create_index("uq_wht_active", "withholding_tables", ["effective_from"], unique=True, schema="hr",
                    postgresql_where=sa.text("status = 'ACTIVE'"))
    rows = _rows()
    bind = op.get_bind()
    bind.execute(sa.text(
        "INSERT INTO hr.withholding_tables (effective_from, status, title, rows, child_deduction, checksum) "
        "VALUES ('2026-03-01', 'CANDIDATE', :t, :r, :c, :h)"),
        {"t": "소득세법 시행령 별표 2 <개정 2026.2.27> 근로소득 간이세액표",
         "r": json.dumps(rows), "c": json.dumps(_CHILD),
         "h": hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()})
    bind.execute(sa.text(
        "INSERT INTO common.pending_decisions (key, category, title, question, options, context_ref) "
        "VALUES (:k, 'HR', :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"),
        {"k": _KEY, "t": "2026-07 소득세 과다 원천징수(간이세액표 미적용) 처리", "q": _Q,
         "o": json.dumps(_OPTS, ensure_ascii=False), "r": _REF})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM common.pending_decisions WHERE key = :k AND status = 'PENDING'"), {"k": _KEY})
    op.drop_index("uq_wht_active", table_name="withholding_tables", schema="hr")
    op.drop_table("withholding_tables", schema="hr")
