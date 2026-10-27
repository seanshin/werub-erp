---
name: fragile-patterns
description: Recurring defect patterns found in services/core ERP code reviews — check these first
metadata:
  type: project
---

Defect patterns observed in the HR/claims/integrations sprint (PR #126~#153). Re-check on every review:

1. **`session.rollback()` inside a savepoint** — `InsuranceReportService.record` (and `his_webhook_router`) call `await session.rollback()` on IntegrityError. When invoked under `session.begin_nested()` (e.g. `inbound_consumer._staff`, `absence_service`), this rolls back the WHOLE session/transaction, not just the savepoint, breaking per-event isolation and undoing already-applied work in the same batch. The savepoint-safe form is `await savepoint.rollback()` or letting the IntegrityError propagate to the `begin_nested` block.
   **Why:** session-scope rollback vs savepoint-scope rollback is silently conflated.

2. **`priced=False` warns "합계 제외" but still sums** — `billing_engine.compute_invoice` appends a "단가 미해소 … 합계 제외" warning yet adds the amount to totals anyway (unit_price 0 ⇒ contributes 0, so masked unless a stale price exists). Warning text contradicts behavior.

3. **Fee `type`/`cap` dropped in the invoice→pre-review path** — `billing_service._fee_map` selects only `unit_price`, so `_pre_review` labels every code `type=TARIFF`. `NONCOVERED_CLAIM` (BLOCK) and DRUG-cap rules can never fire from the live invoice signal, while `worklist.sync_from_billing_lines` (which DOES pass code_type) would flag them. Two paths, divergent results.

**How to apply:** These are the high-value things to grep for in this codebase — `session.rollback` near `begin_nested`, warning strings that don't match control flow, and fee dicts that lose `code_type`/`cap` fields between layers.
