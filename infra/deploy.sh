#!/usr/bin/env bash
# ERP 동거 배포 (P2-2) — 버전 태그 이미지 빌드·기동·헬스검증·구 이미지 세대보관.
# 롤백은 infra/rollback.sh <이전버전>. prod 호스트(2026-09-09~ /home/deploy/his-backup/weruby/hospital-erp)에서 실행.
#
#   ./infra/deploy.sh v1.125.0 [서비스...]
#   서비스 미지정 시 core web worker-batch worker-integration 전체. 코드영역만 바뀌면 일부 지정 가능.
set -euo pipefail

VERSION="${1:?사용법: deploy.sh <version> [service...] (예: deploy.sh v1.125.0 core)}"
shift || true
SERVICES=("$@")
[ ${#SERVICES[@]} -eq 0 ] && SERVICES=(core web worker-batch worker-integration)

KEEP="${ERP_IMAGE_KEEP:-5}"                       # 세대보관 개수(롤백 대상)
COMPOSE="docker compose -f infra/docker-compose.coloc.yml"
export ERP_VERSION="$VERSION"

echo "▶ [$VERSION] 빌드·태깅 — ${SERVICES[*]}"
$COMPOSE build "${SERVICES[@]}"

# 기동 순서 — core 먼저(마이그레이션 완료·health/deep 확인) → 나머지. 종전엔 4개를 동시에 올려 워커가 core의
# 마이그레이션 전에 새 코드로 떴고, 헬스는 상수 200인 /api/health만 봤다(2026-10-09 전체 흐름 검토 H).
#
# 게이트는 deep의 checks.db·checks.redis만 본다(2026-10-09 전체 흐름 검토 2차 H). 종전엔 "status":"ok"를
# 요구했는데 그건 worker-batch 하트비트·정기업무 신선도까지 포함한다 — 최초 설치나 워커가 깨진 상태에선
# core가 멀쩡해도 영원히 degraded(503)라 배포가 core만 새 버전으로 올린 채 멈춰 버전 혼재가 남았다.
# 워커는 이 게이트 뒤에 올라오므로 워커 상태로 core를 막는 것은 순환이다. degraded는 503이라 -f를 쓰지 않는다.
wait_core() {
  echo "▶ core 헬스(deep: db·redis) 대기…"
  local body=""
  for i in $(seq 1 45); do
    body="$(curl -s --max-time 5 http://localhost:8001/api/health/deep || true)"
    if printf '%s' "$body" | python3 -c '
import json, sys
try:
    c = json.load(sys.stdin).get("checks", {})
except Exception:
    sys.exit(1)
sys.exit(0 if c.get("db") == "ok" and c.get("redis") == "ok" else 1)'; then
      echo "  ✔ core 정상(db·redis) — 전체: $body"; return 0
    fi
    sleep 2
  done
  echo "  ✗ core 헬스 실패(db/redis) — 마지막 응답: ${body:-없음}"
  echo "    롤백 검토: infra/rollback.sh <이전버전>"; exit 1
}
OTHERS=()
for s in "${SERVICES[@]}"; do [ "$s" = core ] || OTHERS+=("$s"); done
if printf '%s\n' "${SERVICES[@]}" | grep -qx core; then
  echo "▶ 기동: core"
  $COMPOSE up -d core
  wait_core
fi
if [ ${#OTHERS[@]} -gt 0 ]; then
  echo "▶ 기동: ${OTHERS[*]}"
  # --no-deps: worker-batch 등이 depends_on core라, 없으면 compose가 core를 다시 재생성(방금 검증한
  # core를 재기동·이번 배포에서 core를 지정하지 않았으면 core까지 새 이미지로 교체)한다(2차 H).
  $COMPOSE up -d --no-deps "${OTHERS[@]}"
fi

# latest 태그를 이번 버전으로 — compose의 기본 태그(${ERP_VERSION:-latest})를 쓰는 런북 명령이 옛 이미지로
# 바꿔 끼우던 문제 방지(검토 H)
for svc in "${SERVICES[@]}"; do
  docker tag "infra-$svc:$VERSION" "infra-$svc:latest" 2>/dev/null || true
done

# 구 이미지 세대보관 — 버전 태그(v*)만 최근 $KEEP개 유지, 초과분 정리(latest는 항상 보존)
echo "▶ 구 이미지 세대보관 (최근 $KEEP)"
for svc in core worker-integration worker-batch web; do
  docker images "infra-$svc" --format '{{.Tag}} {{.ID}}' \
    | grep -E '^v[0-9]' | sort -Vr | tail -n +$((KEEP + 1)) | awk '{print $2}' \
    | xargs -r docker rmi -f 2>/dev/null || true
done

echo "$(date -u +%FT%TZ) $VERSION [${SERVICES[*]}]" >> infra/.deploy-history
echo "✔ 배포 완료 $VERSION — 롤백 대상:"
docker images infra-core --format '  infra-core:{{.Tag}}' | grep -E ':v[0-9]' | sort -Vr | head -"$KEEP" || true
