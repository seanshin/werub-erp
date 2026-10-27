"""라우트 등록 스모크 — prefix 중복(이중 prefix) 회귀 방지(404 결함)."""
from erp.main import create_app

EXPECTED = [
    "/api/v1/finance/periods",
    "/api/v1/finance/closing/{fiscal_year}",
    "/api/v1/finance/leases",
    "/api/v1/finance/month-end/{yyyymm}/run",
    "/api/v1/finance/cash-forecast",
    "/api/v1/finance/tax-invoices",
    "/api/v1/finance/tax-invoices/summary",
    "/api/v1/finance/statements/equity-changes",
    "/api/v1/tax/business-status",
    "/api/v1/patients/medical-expenses",   # tax→patient 이관(Phase0)
    "/api/v1/finance/budgets/encumbrances",
    "/api/v1/finance/budgets/amendments",
    "/api/v1/finance/assets/{asset_id}/impair",
    "/api/v1/finance/assets/{asset_id}/revalue",
    "/api/v1/hr/retirement/provisions/{yyyymm}",
    "/api/v1/hr/compensation/{employee_id}",
    "/api/v1/hr/onboarding/pending",
    "/api/v1/hr/payrolls/{yyyymm}/generate-bulk",
    "/api/v1/hr/absences",
    "/api/v1/hr/insurance-reports",
    "/api/v1/hr/compliance/status",
    "/api/v1/hr/employees/{employee_id}/dependents",
    "/api/v1/hr/employees/{employee_id}/offboarding",
    "/api/v1/hr/pay-rates",
    "/api/v1/hr/employees/{employee_id}/resign",
    "/api/v1/inventory/valuation",
    "/api/v1/inventory/shortage",
    "/api/v1/inventory/stock-takes",
    "/api/v1/inventory/turnover",
    "/api/v1/inventory/contracts",
    "/api/v1/inventory/contracts/{contract_id}/accrue/{yyyymm}",
    "/api/v1/costing/department-pl",
    "/api/v1/costing/snapshot",
    "/api/v1/costing/multistage",
    "/api/v1/regulatory/notices/ai-diagnose",
    "/api/v1/claims/pre-review/cases",
    "/api/v1/claims/pre-review/cases/{case_id}",
    "/api/v1/claims/pre-review/cases/{case_id}/statement",
    "/api/v1/claims/pre-review/cases/{case_id}/queries",
    "/api/v1/claims/pre-review/queries",
    "/api/v1/claims/pre-review/queries/{query_id}/replies",
    "/api/v1/claims/pre-review/findings/{finding_id}/decision",
    "/api/v1/claims/pre-review/stats",
    "/api/v1/claims/pre-review/sync-from-lines",
    "/api/v1/claims/ingest/medrlt-json",
    "/api/v1/claims/pre-review/rules",
    "/api/v1/claims/pre-review/cases/bulk-status",
    "/api/v1/claims/pre-review/cases-export.xlsx",
    "/api/v1/regulatory/fees/export.csv",
    "/api/v1/regulatory/nursing-grade/monitor",
    "/api/v1/analytics/executive",
    "/api/v1/integrations/his/{domain}/webhook",
    "/api/v1/integration/hr/payslip",
    "/api/v1/integration/hr/leave-balance",
    "/api/v1/integration/hr/withholding-receipt",
    "/api/v1/integration/billing/invoice",
    "/api/v1/integration/regulatory/fees",
    "/api/v1/integration/regulatory/unmapped-codes",
    "/api/v1/admin/regulatory/drug-map",
    "/api/v1/integration/regulatory/drug-map/confirmed",
]


def test_critical_routes_registered_no_double_prefix():
    paths = {r.path for r in create_app().routes}
    missing = [p for p in EXPECTED if p not in paths]
    assert not missing, f"미등록(이중 prefix 의심): {missing}"
    # 이중 prefix 흔적 없어야
    assert not any("/finance/finance/" in p or "/hr/hr/" in p for p in paths)
