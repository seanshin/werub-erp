"""0231 — G10 XBRL 기초(§3-2 element 태그 선제 도입): accounts.ifrs_element.

⚠ XBRL 인스턴스 생성기·제출 워크플로는 규제 확인(비영리 의료법인 DART 대상 여부) 후 별도
프로젝트(xbrl-regulatory-decision.md §3-3/4). 본 마이그는 결정 브리프 §2 "불확실/장래 대비 —
element 태그만 선제 도입, 생성기는 보류"에 따른 데이터 기초만.

- accounts.ifrs_element: 계정별 표준 IFRS/K-IFRS 택소노미 concept(예: ifrs-full:CashAndCashEquivalents).
  base COA 대표 계정 best-effort 백필(나머지는 커버리지 감사로 완결).

Revision ID: 0231
"""
from alembic import op
import sqlalchemy as sa

revision = "0231"
down_revision = "0230"
branch_labels = None
depends_on = None

# 대표 계정 코드 → IFRS 택소노미 concept(best-effort·감독기관 지정 택소노미 확정 시 교체).
_SEED = {
    "1110": "ifrs-full:CashAndCashEquivalents",
    "1130": "ifrs-full:TradeAndOtherCurrentReceivables",
    "1131": "ifrs-full:TradeAndOtherCurrentReceivables",
    "1132": "ifrs-full:TradeAndOtherCurrentReceivables",
    "1140": "ifrs-full:TradeAndOtherCurrentReceivables",
    "1150": "ifrs-full:Inventories",
    "1190": "ifrs-full:AllowanceAccountForCreditLossesOfFinancialAssets",
    "2110": "ifrs-full:TradeAndOtherCurrentPayables",
    "2112": "ifrs-full:TradeAndOtherCurrentPayables",
    "2120": "ifrs-full:NoncurrentProvisionsForEmployeeBenefits",
    "2135": "ifrs-full:CurrentTaxLiabilitiesCurrent",
    "2150": "ifrs-full:TradeAndOtherCurrentPayables",
    "3300": "ifrs-full:RetainedEarnings",
    "3410": "ifrs-full:OtherReserves",
    "4110": "ifrs-full:Revenue",
    "4115": "ifrs-full:Revenue",
    "4120": "ifrs-full:Revenue",
    "4130": "ifrs-full:OtherIncome",
    "5310": "ifrs-full:EmployeeBenefitsExpense",
    "5330": "ifrs-full:EmployeeBenefitsExpense",
    "5410": "ifrs-full:RawMaterialsAndConsumablesUsed",
    "5525": "ifrs-full:OtherExpenseByNature",
    "5540": "ifrs-full:OtherExpenseByNature",
    "5690": "ifrs-full:ImpairmentLossReversalOfImpairmentLossRecognisedInProfitOrLoss",
    "5720": "ifrs-full:FinanceCosts",
    "5910": "ifrs-full:OtherExpenseByNature",
}


def upgrade() -> None:
    op.add_column("accounts", sa.Column("ifrs_element", sa.String(80), nullable=True),
                  schema="finance")
    conn = op.get_bind()
    for code, elem in _SEED.items():
        conn.execute(sa.text(
            "UPDATE finance.accounts SET ifrs_element = :e "
            "WHERE code = :c AND ifrs_element IS NULL"), {"e": elem, "c": code})


def downgrade() -> None:
    op.drop_column("accounts", "ifrs_element", schema="finance")
