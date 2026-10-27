#!/usr/bin/env bash
# ERP 롤백 (P2-2) — 이전 버전 태그 이미지로 재기동(재빌드 없음). prod 호스트에서 실행.
#
#   ./infra/rollback.sh              # 가용 롤백 대상 목록
#   ./infra/rollback.sh v1.124.0     # 해당 버전으로 롤백(확인 프롬프트)
#   ./infra/rollback.sh v1.124.0 -y  # 무확인
#
# ⚠ 마이그레이션 (2026-10-09 전체 흐름 검토 2차 H — 이미지 세대별로 다르다):
#   - 롤백 대상이 v1.289.0 이상: core CMD가 `python -m erp.migrate_on_start` — DB가 이미지보다 앞서 있으면
#     upgrade를 건너뛰고 기동한다 → 코드만 롤백(다운그레이드 불요·데이터 보존). 신 스키마가 구 코드를
#     실제로 깨뜨릴 때만 다운그레이드.
#   - 롤백 대상이 v1.288.0 이하: CMD가 `alembic upgrade head && uvicorn`이라 모르는 리비전이면 기동 실패 →
#     먼저 수동 `alembic downgrade <구이미지 head>` 필요.
#   - 다운그레이드 전 백업: 0251 downgrade는 퇴사자 접근차단 표식(hr.employees.access_revoked_on)을, 0250은
#     법정 요율(hr.statutory_params)을 지운다 → `pg_dump -t`/COPY로 먼저 떠 둘 것.
#   (런북 docs/05-operations/rollback-deploy.runbook.md)
set -euo pipefail

COMPOSE="docker compose -f infra/docker-compose.coloc.yml"

if [ "${1:-}" = "" ]; then
  echo "가용 롤백 대상 (infra-core 버전 태그):"
  docker images infra-core --format '{{.Tag}} ({{.CreatedSince}})' \
    | grep -E '^v[0-9]' | sort -Vr | sed 's/^/  /'
  echo "사용법: rollback.sh <version> [-y]"
  exit 0
fi

TARGET="$1"
ASSUME_YES="${2:-}"
docker image inspect "infra-core:$TARGET" >/dev/null 2>&1 \
  || { echo "✗ infra-core:$TARGET 이미지 없음 — 위 목록에서 선택(rollback.sh 인자 없이 실행)"; exit 1; }

echo "현재 DB 리비전:"
docker exec infra-core-1 alembic current 2>/dev/null | tail -1 || echo "  (확인 불가)"
# 대상 이미지가 migrate_on_start(≥v1.289.0)인지 이미지 CMD로 판정 — 버전 문자열 비교보다 확실하다.
if docker image inspect "infra-core:$TARGET" --format '{{json .Config.Cmd}}' 2>/dev/null \
    | grep -q 'migrate_on_start'; then
  echo "ℹ $TARGET 은 migrate_on_start 이미지 — DB가 앞서 있으면 upgrade를 건너뛰고 기동(코드만 롤백·downgrade 불요)."
else
  echo "⚠ $TARGET 은 구 CMD(alembic upgrade head) 이미지 — 이후 마이그레이션 추가 릴리즈가 있었다면"
  echo "  먼저 수동 'alembic downgrade <구이미지 head>' 필요. 0251↓=퇴사자 차단 표식 삭제·0250↓=법정 요율 삭제"
  echo "  → 다운그레이드 전 pg_dump -t hr.statutory_params 등으로 백업(런북)."
fi

if [ "$ASSUME_YES" != "-y" ]; then
  read -rp "$TARGET 로 롤백하시겠습니까? [y/N] " ans
  [ "$ans" = "y" ] || { echo "취소"; exit 0; }
fi

export ERP_VERSION="$TARGET"
$COMPOSE up -d --no-build core web worker-batch worker-integration

echo "▶ core 헬스 대기…"
for i in $(seq 1 30); do
  curl -sf http://localhost:8001/api/health >/dev/null 2>&1 && { echo "  ✔ core 정상"; break; }
  [ "$i" = 30 ] && { echo "  ✗ core 헬스 실패 — 마이그 불일치 가능(런북의 downgrade 절차 확인)"; exit 1; }
  sleep 2
done

echo "$(date -u +%FT%TZ) ROLLBACK→$TARGET" >> infra/.deploy-history
echo "✔ 롤백 완료 → $TARGET"
