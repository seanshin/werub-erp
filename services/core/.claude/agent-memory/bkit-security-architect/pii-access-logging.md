---
name: pii-access-logging
description: ERP PII access-logging expectations — where record_access/record_audit is expected on sensitive reads
metadata:
  type: project
---

ERP convention (from PR#115 baseline): reads that expose patient `chart_no` or employee payroll/compensation PII should call `AuditService.record_access` (VIEW/PRINT/EXPORT); mutations to money/policy-affecting data should call `record_audit` (before/after).

Observed pattern (verify per file, may change):
- **Has access logging:** `claims/router.py` `/statements` (chart_no), `/cases-export.xlsx`; `hr/router.py` `/employees*`, `/payrolls/me/*`, `/certificates/*/pdf`.
- **Missing access logging (findings):** `integrations/billing_api.py` `/invoice` (HIS pulls patient billing by chartNo — no record_access); `integrations/ess_router.py` payslip/withholding/leave-balance (HIS reads employee payroll by employeeNumber — no record_access); `hr/router.py` `/compensation/{id}` GET and `/employees/{id}/dependents` GET (sensitive but no record_access).

**PII storage:** `CompensationProfile.bank_account_enc` is encrypted (AES column w/ AAD `column_aad`, masked-only on read — good). Dependents (가족) stored plaintext (name/relation/birth_date) — flagged as encryption-worthiness question. `BillingLine` stores chart_no + patient_age + patient_sex plaintext from HIS webhook.

Update (verified 2026-06-15): `hr/router.py` GET `/compensation/{id}` (line 732) and `/employees/{id}/dependents` (line 806) NOW call `record_access` — previously-flagged gap is closed. `claims/router.py /statements`, scm narcotics ledger view/export, hr /employees all have record_access too.

Row-level scope NOT applied (verified 2026-06-15, OWASP A01): several list/aggregate EPs call `require_scope` (role check) but DISCARD the returned scope, so DEPT/SELF callers see all rows. Confirmed: `finance/router.py::budget_amendments` (line 138, user-controlled `department_id` param bypasses scope.dept_id), `patient/router.py::search_patients` (line 43, `_patients.search` has no scope param — but `list_receptions` line 81 DOES filter, good contrast), `hr/router.py::list_absences` (uses hr.employees gate = MANAGER DEPT/STAFF SELF, `absence_service.list` has no scope param), `scm narcotics ledger` (MANAGER DEPT but queried by item_id, lower practical impact). Correct reference patterns: `hr list_employees` (line 66), `patient list_receptions` (line 94, dept_code_candidates), `finance budget_execution` (line 155).

Centralized-audit gap (A09, Medium not Critical): many sensitive mutations rely on domain-LOCAL audit fields instead of `AuditService.record_audit` — narcotics `add_entry`/`correct` (SHA-256 hash chain + handler_id, no record_audit), `period_service.close/reopen` (closed_by/note fields, no record_audit — reopen is a control reversal), voucher create, budget amend, payroll confirm. claims reduction decision DOES call record_audit (worklist_service ~710). Don't overcount these as separate Criticals — frame as one A09 incompleteness item.

IDOR on hr `{id}` EPs (compensation/dependents/offboarding) is LOW: those gates (`hr.compensation`, `hr.payroll write`) grant ALL-scope only (no DEPT/SELF in policy.py) so only ADMIN/DIRECTOR reach them — ownership re-check is moot. `_assert_employee_access` IS used on the SELF-reachable EPs (payroll/severance/year-end).

**How to apply:** New EP returning chart_no or payroll/compensation/dependent PII → check for record_access; absence is a Medium finding (logging/monitoring gap, OWASP A09). For list/aggregate EPs, the test is not "does it call require_scope" but "does it APPLY scope.kind (DEPT/SELF) as a WHERE clause" — discarded scope is an A01 finding. Enumeration: employeeNumber/chartNo are guessable external identifiers on the integration EPs — note rate-limit/access-log absence.
