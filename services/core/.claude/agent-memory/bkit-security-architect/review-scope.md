---
name: review-scope
description: Ground rules for ERP sprint security reviews — read-only, findings-only, severity scale, PR#115 baseline
metadata:
  type: project
---

ERP (`/Users/user/GitHub_project/ERP`, core at `services/core`) sprint security reviews are **read-only**: report findings only, never modify code.

**Why:** User runs these as a recurring per-sprint gate (phase-7-seo-security / code-review skill). They want a severity-ranked findings list (Critical/High/Medium/Low) with `file:line` + one-line attack scenario + recommendation, OWASP mapping only when it clarifies.

**How to apply:** Carry forward prior-review baselines as accepted controls and check they aren't regressed. PR #115 baseline established: PII access gate, RBAC policy registration (every EP's (resource,action) must exist in `common/policy.py` POLICY — unregistered = 403 fail-closed via `require_scope`), callback idempotency, CSV row-isolation defense. Sprint reviewed at PR#126–#153 (2026-06).

Severity → action: Critical=block deploy; High=fix before release; Medium=next sprint; Low=backlog.
