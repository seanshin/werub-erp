"""0266 — 입원 중간정산 수납 정비 수신(HIS v4.116.0·요청서 2026-10-22): 수단·무효·환불.

- patient.interim_billings: payments(수단별 원장 줄 원문)·paid_by·산정 근거(days·daily_rate·price_basis·copay_rate)·
  무효(voided_at·void_reason). 상태 GENERATED|PAID|VOID.
- patient.interim_billing_refunds: 중간정산 환불(billing.interim_refunded·refund_id 멱등·부분 환불 여러 건).
- 결정 대기 1건(FINANCE): 재원 중 1131 대변 잔액(선수금 성격) 처리 — HIS 요청 R2(7/24 위임 건).

Revision ID: 0266
Revises: 0265
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0266"
down_revision = "0265"
branch_labels = None
depends_on = None

_COLS = [
    sa.Column("payments", sa.JSON(), nullable=True),
    sa.Column("paid_by", sa.String(64), nullable=True),
    sa.Column("days", sa.Integer(), nullable=True),
    sa.Column("daily_rate", sa.Numeric(14, 0), nullable=True),
    sa.Column("price_basis", sa.String(20), nullable=True),
    sa.Column("copay_rate", sa.Numeric(5, 3), nullable=True),
    sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("void_reason", sa.String(200), nullable=True),
]

_REF = "docs/05-reference/erp-reply-his-interim-settlement-2026-10-22.md"
SEEDS = [
    ("interim-1131-credit-reclass", "FINANCE", "입원 중간정산 수납의 1131 대변 잔액(선수금 성격)을 어떻게 표시할지",
     "입원 중간정산 수납(HIS billing.interim_paid)은 게이트(his_interim_gl_enabled)를 켜면 DR 현금성 / CR 1131 환자미수금으로 "
     "전기되고, 입원료 매출(DR 1131)은 퇴원 후 발생 인식(patient_accrual_enabled)에서 온다. 그래서 재원 중에는 1131이 환자별로 "
     "일시 대변 잔액(받은 돈이 매출보다 먼저)이 된다. "
     "게이트를 켜기 전에 정해야 한다 — 1131 순액 그대로 둘지, 별도 선수금 계정으로 받을지. HIS 이벤트는 어느 쪽이든 바뀌지 않는다.",
     [{"key": "KEEP_1131", "label": "1131 순액 유지 · 결산 때 대변 잔액이 크면 수기 재분류 전표",
       "description": "개발 없음. 재원 중 일시 대변은 같은 환자 미수금과 상계되는 성격이라 순액 표시. 월말 재원 환자 대변 잔액 합계가 "
                      "중요하면 결산 조정 전표로 선수금 재분류", "recommended": True},
      {"key": "ADVANCE_ACCOUNT", "label": "별도 계정 '선수금(입원 중간정산)'으로 받고 퇴원 때 1131과 상계",
       "description": "개발 필요 — 계정 신설·interim_paid 대변 변경·퇴원(discharge_settlement) 때 상계 전표·환불 역분개 계정 변경"}]),
]


def upgrade() -> None:
    for c in _COLS:
        op.add_column("interim_billings", c, schema="patient")
    op.create_table(
        "interim_billing_refunds",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("refund_id", sa.String(64), nullable=False),
        sa.Column("interim_billing_id", sa.String(64), nullable=False, index=True),
        sa.Column("chart_no", sa.String(20), nullable=False, index=True),
        sa.Column("amount", sa.Numeric(14, 0), nullable=False),
        sa.Column("method", sa.String(10), nullable=False),
        sa.Column("reason", sa.String(200), nullable=True),
        sa.Column("refunded_by", sa.String(64), nullable=True),
        sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("refund_id", name="uq_interim_refund_id"),
        sa.CheckConstraint("amount > 0", name="ck_interim_refund_amount"),
        sa.CheckConstraint("method IN ('CASH','CARD','TRANSFER')", name="ck_interim_refund_method"),
        schema="patient",
    )
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
    op.drop_table("interim_billing_refunds", schema="patient")
    for c in reversed(_COLS):
        op.drop_column("interim_billings", c.name, schema="patient")
