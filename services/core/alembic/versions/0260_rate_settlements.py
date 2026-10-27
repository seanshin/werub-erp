"""0260 — 요율 차액 정산(결정=실행) 테이블·소급정산 공제 항목·결정문 안내 (2026-10-10).

결정 대기 `statutory-2026-rates-check`(2026-07 급여 요율 차액)는 선택지만 있고 실행 수단이 없었다. 관리자 > 급여 설정
'요율 차액 정산'에서 방식을 고르면 급여 공제 줄·전표·결정 기록이 한 트랜잭션으로 바뀐다(hr.rate_settlement).
결정문은 PENDING일 때만 안내 문장을 덧붙인다(DECIDED 결정문은 근거라 불변).

Revision ID: 0260
Revises: 0259
"""
import sqlalchemy as sa
from alembic import op

revision = "0260"
down_revision = "0259"
branch_labels = None
depends_on = None

_KEY = "statutory-2026-rates-check"
_TAIL = ("\n실행: 관리자 > 급여 설정 '요율 차액 정산'에서 방식을 고르면 미리보기(직원별 처분·전표 금액) 후 실행되고, "
         "이 결정도 함께 기록됩니다(결정 대기함에서 직접 결정하지 않음). 실행 취소는 공제가 확정되기 전까지 가능.")
_CODES = [("NP_ADJ", "국민연금 소급정산", 54), ("HI_ADJ", "건강보험 소급정산", 55),
          ("LTC_ADJ", "장기요양 소급정산", 56), ("EI_ADJ", "고용보험 소급정산", 57)]


def _ym(col: str) -> str:
    digit = "('0','1','2','3','4','5','6','7','8','9')"
    digits = " AND ".join(f"substr({col}, {i}, 1) IN {digit}" for i in (1, 2, 3, 4, 6, 7))
    months = "(" + ",".join(f"'{m:02d}'" for m in range(1, 13)) + ")"
    return (f"length({col}) = 7 AND substr({col}, 5, 1) = '-' AND {digits} "
            f"AND substr({col}, 6, 2) IN {months}")


def upgrade() -> None:
    op.create_table(
        "rate_settlements",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source_yyyymm", sa.String(7), nullable=False),
        sa.Column("method", sa.String(24), nullable=False),
        sa.Column("target_yyyymm", sa.String(7)),
        sa.Column("status", sa.String(10), nullable=False, server_default="ACTIVE"),
        sa.Column("decision_key", sa.String(64)),
        sa.Column("note", sa.String(1000), nullable=False),
        sa.Column("employee_total", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("employer_total", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("absorbed_total", sa.Numeric(14, 0), nullable=False, server_default="0"),
        sa.Column("voucher_ref", sa.String(64)),
        sa.Column("rates", sa.JSON),
        sa.Column("created_by", sa.BigInteger),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("canceled_by", sa.BigInteger),
        sa.Column("canceled_at", sa.DateTime(timezone=True)),
        sa.Column("cancel_reason", sa.String(500)),
        sa.CheckConstraint("method IN ('NEXT_PAYROLL_ADJUST','EMPLOYER_ABSORB','NOT_PAYROLL_SOT')",
                           name="ck_rate_settle_method"),
        sa.CheckConstraint("status IN ('ACTIVE','CANCELED')", name="ck_rate_settle_status"),
        sa.CheckConstraint(_ym("source_yyyymm"), name="ck_rate_settle_source"),
        schema="hr",
    )
    op.create_index("uq_rate_settle_active", "rate_settlements", ["source_yyyymm"], unique=True,
                    schema="hr", postgresql_where=sa.text("status = 'ACTIVE'"))
    op.create_table(
        "rate_settlement_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("settlement_id", sa.BigInteger, sa.ForeignKey("hr.rate_settlements.id"), nullable=False),
        sa.Column("employee_id", sa.BigInteger, sa.ForeignKey("hr.employees.id"), nullable=False),
        sa.Column("source_record_id", sa.BigInteger, nullable=False),
        sa.Column("np", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("hi", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("ltc", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("ei", sa.Numeric(12, 0), nullable=False, server_default="0"),
        sa.Column("disposition", sa.String(12), nullable=False),
        sa.Column("applied_record_id", sa.BigInteger),
        sa.Column("applied_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("settlement_id", "employee_id", name="uq_rate_settle_item"),
        schema="hr",
    )
    op.create_index("ix_hr_rate_settlement_items_settlement_id", "rate_settlement_items", ["settlement_id"],
                    schema="hr")
    op.create_index("ix_hr_rate_settlement_items_employee_id", "rate_settlement_items", ["employee_id"],
                    schema="hr")
    bind = op.get_bind()
    for code, name, order in _CODES:
        bind.execute(sa.text(
            "INSERT INTO hr.pay_item_catalog (code, name, kind, category, taxable, display_order, is_medical) "
            "VALUES (:c, :n, 'DEDUCTION', '법정공제', false, :o, false) ON CONFLICT (code) DO NOTHING"),
            {"c": code, "n": name, "o": order})
    bind.execute(sa.text(
        "UPDATE common.pending_decisions SET question = question || :t WHERE key = :k AND status = 'PENDING' "
        "AND position(:m in question) = 0"), {"t": _TAIL, "k": _KEY, "m": "요율 차액 정산"})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text(
        "UPDATE common.pending_decisions SET question = replace(question, :t, '') WHERE key = :k AND status = 'PENDING'"),
        {"t": _TAIL, "k": _KEY})
    bind.execute(sa.text("DELETE FROM hr.pay_item_catalog WHERE code IN ('NP_ADJ','HI_ADJ','LTC_ADJ','EI_ADJ')"))
    op.drop_index("ix_hr_rate_settlement_items_employee_id", table_name="rate_settlement_items", schema="hr")
    op.drop_index("ix_hr_rate_settlement_items_settlement_id", table_name="rate_settlement_items", schema="hr")
    op.drop_table("rate_settlement_items", schema="hr")
    op.drop_index("uq_rate_settle_active", table_name="rate_settlements", schema="hr")
    op.drop_table("rate_settlements", schema="hr")
