---
name: codebase-shape
description: Architecture conventions of services/core ERP — how money/transaction/engine code is structured
metadata:
  type: project
---

services/core (hospital-erp `core` service) is async SQLAlchemy + FastAPI.

Recurring conventions (derived, verify before relying):
- Calculation engines (`payroll_rules.build_payroll`, `billing_engine.compute_invoice`, `pre_review.PreReviewEngine`, allocation) are **pure functions** — caller injects rates/fee maps loaded from DB. Engine has no DB access.
- Money columns are `Numeric(*,0)` (won, integer). Code compares via `round(x)` to dodge float equality. Engines round per-line with `int(round(...))`.
- Vouchers via `VoucherService.create(source_module, source_ref, lines=[VoucherLineDTO(account, side, amount)])`. Idempotency = UNIQUE on (source_module, source_ref).
- Idempotency pattern across services: pre-check `select` for existing row, return early. HR/insurance also catch `IntegrityError` for concurrency.
- Router gate: every endpoint calls `_scope(user, resource, action)` = `require_scope`; row-scope synthesized explicitly (design §6.2). Heavy use of lazy `from ... import` inside handlers (intentional, to limit import graph).
- HIS inbound: `inbound_consumer.consume` runs each handler inside `session.begin_nested()` savepoint for per-event isolation; `his_inbound_events.source_ref` UNIQUE gives idempotency.

**How to apply:** When reviewing money paths, check engine purity is preserved (no DB in engine), rounding happens once, and voucher debit==credit. When reviewing concurrency, the canonical idempotency is pre-check + IntegrityError catch.
