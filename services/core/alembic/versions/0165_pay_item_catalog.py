"""0165 — 급여 항목코드 카탈로그 + 임금 지급일 (급여명세서 법정 부합 P0).

hr.pay_item_catalog(원본코드→한글명·과세·표시순 SoT) + hr.payroll_records.paid_on(임금 지급일·
법정 §27의2 2호). 카탈로그 시드는 마이그에서 1회 적재(alembic 버전추적으로 재실행 없음). 신규 코드는
후속 마이그로 추가. paid_on은 nullable(과거 확정분 무백필).

Revision ID: 0165
"""
import sqlalchemy as sa
from alembic import op

revision = "0165"
down_revision = "0164"
branch_labels = None
depends_on = None

# (code, name, name_en, kind, category, taxable, nontax_limit, display_order, is_medical)
_SEED = [
    ("BASE", "기본급", "Base pay", "EARNING", "기본급", True, None, 10, False),
    ("BONUS", "정기상여금", "Regular bonus", "EARNING", "상여", True, None, 15, False),
    ("OT", "연장근로수당", "Overtime", "EARNING", "수당", True, None, 20, False),
    ("HOLIDAY", "휴일근로수당", "Holiday work", "EARNING", "수당", True, None, 21, False),
    ("NIGHT_PREMIUM", "야간근로가산", "Night premium", "EARNING", "수당", True, None, 22, False),
    ("SHIFT", "교대수당", "Shift allowance", "EARNING", "수당", True, None, 23, False),
    ("NIGHT", "교대수당", "Shift allowance", "EARNING", "수당", True, None, 23, False),  # 레거시 코드
    ("DUTY", "당직수당", "On-call duty", "EARNING", "수당", True, None, 24, False),
    ("QUAL", "자격수당", "Qualification", "EARNING", "수당", True, None, 25, False),
    ("WATCH", "감시단속수당", "Watch allowance", "EARNING", "수당", True, None, 26, False),
    ("MEAL", "식대", "Meal allowance", "EARNING", "비과세", False, 200000, 27, False),
    ("INCENTIVE", "성과급", "Incentive", "EARNING", "성과", True, None, 30, True),
    ("UNPAID_LEAVE", "무급휴가 공제", "Unpaid leave", "EARNING", "조정", True, None, 40, False),
    ("NP", "국민연금", "National pension", "DEDUCTION", "법정공제", False, None, 50, False),
    ("HI", "건강보험", "Health insurance", "DEDUCTION", "법정공제", False, None, 51, False),
    ("LTC", "장기요양보험", "Long-term care", "DEDUCTION", "법정공제", False, None, 52, False),
    ("EI", "고용보험", "Employment insurance", "DEDUCTION", "법정공제", False, None, 53, False),
    ("TAX", "소득세", "Income tax", "DEDUCTION", "세금", False, None, 60, False),
    ("LOCALTAX", "지방소득세", "Local income tax", "DEDUCTION", "세금", False, None, 61, False),
]


def upgrade() -> None:
    op.add_column("payroll_records", sa.Column("paid_on", sa.Date(), nullable=True),
                  schema="hr")
    cat = op.create_table(
        "pay_item_catalog",
        sa.Column("code", sa.String(20), primary_key=True),
        sa.Column("name", sa.String(40), nullable=False),
        sa.Column("name_en", sa.String(60), nullable=True),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("category", sa.String(20), nullable=True),
        sa.Column("taxable", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("nontax_limit", sa.Numeric(12, 0), nullable=True),
        sa.Column("display_order", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("is_medical", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("kind IN ('EARNING','DEDUCTION')", name="ck_pay_item_kind"),
        schema="hr",
    )
    op.bulk_insert(cat, [
        {"code": c, "name": n, "name_en": en, "kind": k, "category": cat_,
         "taxable": tx, "nontax_limit": lim, "display_order": order, "is_medical": med}
        for c, n, en, k, cat_, tx, lim, order, med in _SEED
    ])


def downgrade() -> None:
    op.drop_table("pay_item_catalog", schema="hr")
    op.drop_column("payroll_records", "paid_on", schema="hr")
