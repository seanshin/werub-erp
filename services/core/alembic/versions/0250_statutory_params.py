"""0250 — 법정 파라미터(4대보험 요율·소득세 승계)를 관리화면에서 등록.

`hr/statutory.py`의 RATES·TAX_PARAMS가 2025·2026만 등록돼 있어 **2027-01-01부터 prod 원천징수가
fail-safe로 정지**할 예정이었다. 고시값은 매년 바뀌는 **사람이 확인할 사실**이라 코드 배포에 묶지 않고
관리자가 근거(고시 번호)와 함께 등록한다(DB가 코드 상수보다 우선).

결정 대기함에 `statutory-2026-rates-check`도 올린다 — 코드의 2026 4대보험 요율이 2025와 **완전히 같다**
(국민연금 4.5%·건강 3.545%·장기요양 12.95%). 2026년엔 국민연금 보험료율 인상(개정 국민연금법)과
건강보험료율 인상이 있었던 것으로 알려져 있어 복사 누락 가능성이 있다 — 고시 대조가 필요하다
(코드가 단정하지 않는다).

Revision ID: 0250
Revises: 0249
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0250"
down_revision = "0249"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "statutory_params",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("values", sa.JSON(), nullable=False),
        sa.Column("source_note", sa.String(500), nullable=False),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("year", "kind", name="uq_statutory_params_year_kind"),
        sa.CheckConstraint("kind IN ('INSURANCE','TAX_CARRY')", name="ck_statutory_params_kind"),
        schema="hr",
    )
    op.get_bind().execute(sa.text(
        "INSERT INTO common.pending_decisions (key, category, title, question, options, context_ref) "
        "VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"), {
        "k": "statutory-2026-rates-check", "c": "GENERAL",
        "t": "2026년 4대보험 요율이 2025년 값과 동일 — 고시 대조 필요",
        "q": "코드에 등록된 2026 근로자 요율(국민연금 4.5%·건강보험 3.545%·장기요양 12.95%·연금 하한 "
             "390,000원)이 2025년과 완전히 같습니다. 2026년에는 개정 국민연금법에 따른 보험료율 인상과 "
             "건강보험료율 인상이 있었던 것으로 알려져 있습니다 — 검토 시 **후보값**(원문 미확인): 국민연금 "
             "4.75%(9.5%의 절반)·건강보험 3.595%(7.19%)·장기요양 약 13.14%·연금 하한 400,000원(2025-07 고시). "
             "먼저 prod에서 ERP가 2026 급여를 실제로 계산했는지(급여 레코드 건수) 확인하세요. 고시를 대조해 맞으면 '현행 확인', 다르면 관리화면 /admin/payroll-settings "
             "'법정 요율'에 2026 값을 등록하세요(이후 생성분부터 적용 — 이미 지급한 달의 차액 정산은 공단 정산"
             "·급여 소급 절차로 별도 처리).",
        "o": json.dumps([
            {"key": "REGISTER", "label": "고시값 등록(차이 있음)",
             "description": "관리화면에 2026 고시 요율 등록 — 이후 급여부터 반영·소급 차액 별도 정산",
             "recommended": True},
            {"key": "CONFIRMED", "label": "현행 확인(차이 없음)",
             "description": "고시 대조 결과 코드 값이 맞음 — 근거를 결정 메모에 기록"}],
            ensure_ascii=False),
        "r": "services/core/src/erp/hr/statutory.py RATES"})


def downgrade() -> None:
    op.get_bind().execute(sa.text(
        "DELETE FROM common.pending_decisions WHERE key='statutory-2026-rates-check' "
        "AND status='PENDING'"))
    op.drop_table("statutory_params", schema="hr")
