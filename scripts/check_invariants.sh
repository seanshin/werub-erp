#!/usr/bin/env bash
# ARCHITECTURE-INVARIANTS CI 게이트 (배치중립 Phase0·HIS 공용 방법론 재사용)
# 대응: INV-TEL-01(텔레메트리·로그 위생) — 상세 근거 docs/03-analysis/architecture-invariants-audit-erp-2026-07-23.analysis.md
set -uo pipefail
fail=0

# T1. 제3자 텔레메트리 유입 차단 (백엔드 lock·FE deps)
if grep -riE 'sentry|datadog|newrelic|posthog|amplitude|mixpanel|logrocket|fullstory|hotjar|bugsnag|rollbar|@vercel/analytics' \
    services/core/requirements.lock apps/web/package.json 2>/dev/null; then
  echo "FAIL[T1]: 제3자 텔레메트리 SDK 유입 — INV-TEL-01"; fail=1
fi

# T2. FE 외부 URL 차단 (동일 오리진만 — w3.org 네임스페이스 예외)
if grep -rE 'https?://' apps/web/src --include='*.ts' --include='*.tsx' \
    | grep -vE 'localhost|127\.0\.0\.1|w3\.org'; then
  echo "FAIL[T2]: FE 외부 URL — PHI 브라우저 경로는 동일 오리진만"; fail=1
fi

# T3. 로그에 환자 식별자 금지
if grep -rnE 'logger\.(info|warning|error|exception)' services/core/src --include='*.py' \
    | grep -E 'chart_no|"phone"|rrn|patient_name'; then
  echo "FAIL[T3]: 로그 ctx에 환자 식별자 — INV-TEL-01"; fail=1
fi

# T4. 무절단 예외 로깅 금지 (페이로드 값 로그 유입 방지 — [:N] 절단 강제)
if grep -rnE '"err": str\(exc\)[,}]' services/core/src --include='*.py'; then
  echo "FAIL[T4]: 무절단 str(exc) 로깅 — [:120] 절단 필요"; fail=1
fi

# T5. GitHub-hosted 러너 금지 — private 저장소의 hosted 분(minutes)은 과금된다.
#     2026-07 실측: 이 저장소가 hosted Linux 19,078분을 태워 **$102.47 실청구**됐다
#     (무료 2,000분 초과분). 이후 self-hosted(werub-prod)로 옮겨 8·9월 청구 $0.
#     ⚠ macOS 러너는 분당 10배로 차감돼 200분이면 월 무료 한도를 통째로 태운다.
#     러너 라벨이 안 맞으면 잡은 큐에 머무를 뿐 hosted로 폴백하지 않는다 — 안전한 실패다.
if grep -rnE '^\s*runs-on:' .github/workflows 2>/dev/null | grep -vE 'self-hosted'; then
  echo "FAIL[T5]: GitHub-hosted 러너 — private 저장소는 분 과금. self-hosted 라벨만 사용"; fail=1
fi

# T6. Actions 캐시·아티팩트 금지 — 저장 용량 과금 표면(무료 한도 초과 시 청구)
if grep -rnE 'actions/(cache|upload-artifact|download-artifact)' .github/workflows 2>/dev/null; then
  echo "FAIL[T6]: Actions 캐시/아티팩트 — 저장 용량 과금. 사용 금지"; fail=1
fi

if [ "$fail" -eq 0 ]; then echo "invariants gate: OK (T1~T6)"; fi
exit $fail
