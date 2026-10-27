---
name: his-integration-auth
description: HIS server-to-server integration auth + SSRF model in ERP — shared key, HMAC callback, host allowlist
metadata:
  type: reference
---

ERP↔HIS server-to-server integration security model (verify current state before relying — config in `src/erp/config.py`):

- **Inbound auth = `X-Integration-Key` header** compared against `settings.his_webhook_key` via `hmac.compare_digest` (timing-safe). Fails closed when key unset (raises ForbiddenError "미설정"). Used by: `integrations/ess_router.py::_auth`, `integrations/billing_api.py::_auth`, `integrations/his_webhook_router.py`. These three duplicate the same `_auth` block — a change to the auth rule must touch all.
- **e-approval callback** (`integrations/eapproval_router.py`) is unauthenticated-by-header; trust is **HMAC `X-Signature`** (`sha256=<hex>`) verified by `integrations/eapproval.py::verify_signature` (timing-safe). Secret = `eapproval_callback_secret`.
- **Outbound SSRF control = hostname allowlist.** `EApprovalClient.__init__` and `his_billing.py::backfill_lines` validate `urlparse(base).hostname` against an allowlist (`his.example.org`/localhost/127.0.0.1) before HTTP. `his_api_base` default = `https://his.example.org`, server-controlled config.
- RESOLVED (verified 2026-06-15): `hr/eapproval_service.py::_relay_to_his` now DOES the `urlparse(his_api_base).hostname` allowlist check (lines ~102-107) like other outbound paths. Do not re-flag — the SSRF gap is closed.
- Still-open inbound finding (verified 2026-06-15): the SAME `his_webhook_key` authenticates 4 distinct trust domains — `integrations/ess_router.py` (employee payslip/withholding/leave), `billing_api.py` (patient invoice), `his_webhook_router.py` (finance/materials/assets/procurement/hr webhooks), `regulatory_api.py` (fee/drug masters). No per-domain key separation: one key leak forges any domain. Key-domain-separation finding (A05/A07).
- `his_webhook_router.py` replay protection = DB UNIQUE on `source_ref` only (no timestamp-skew window or nonce), unlike `eapproval_router.py` which has both (skew + Redis nonce, fixed 2026-06-14). Weaker-but-present idempotency.

**How to apply:** When reviewing new integration EPs, confirm they reuse one of these auth gates and (for outbound) the host allowlist. New outbound HTTP without host validation is a finding.
