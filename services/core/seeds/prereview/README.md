# 사전심사 룰 시드 (SAMPLE)

`scripts/seed_prereview_rules.py`가 읽는 룰 데이터 디렉터리입니다.

## ⚠️ 이 디렉터리는 구조 예시(SAMPLE)입니다
동봉 데이터는 **JSON 형식·필드를 보여주는 자리표시자**(`SMP-*` 코드, `[샘플]` 사유)입니다.
실제 청구에 의미 있게 발화하려면 **권위 있는 공개데이터로 교체**해야 합니다:

| 파일 | 룰 | 출처(권위) |
|---|---|---|
| `ingredient_map.json` | 보험코드→주성분 | 약가마스터·식약처 DUR 품목정보(data.go.kr/15059486) |
| `dur_rules.json` | 병용금기·중복·연령금기 | 식약처 DUR 병용금기 약물쌍(15089525, 라이선스 유형2 출처표시·비상업) |
| `dx_rules.json` | 진단(KCD)-수가 연계 | HIRA 상병-수가 기준(형식 점검 — 임상타당성은 의료진 게이트) |
| `bundling_rules.json` | 묶음/언번들링 | HIRA 패키지 수가·재료 포함 기준 |
| `surcharge_rates.json` | 종별가산율 | HIRA 고시 종별가산율(의원15·병원20·종합25·상급30 — **고시 시점 확인**) |

## 사용
```
cd services/core
.venv/bin/python scripts/seed_prereview_rules.py --dry-run        # 검증·집계만
.venv/bin/python scripts/seed_prereview_rules.py                  # 멱등 적재(commit)
.venv/bin/python scripts/seed_prereview_rules.py --replace-source MFDS   # DUR 출처 전체 교체(고시 갱신)
```
적재는 멱등(자연키 upsert) — 재실행 안전. 적재 후 기존 청구 반영은 **재-sync** 필요
(런북 `docs/05-reference/prereview-rules-data-load.md` §5).

## SAMPLE 제거
```sql
DELETE FROM claims.dur_rules WHERE source = 'SAMPLE';
DELETE FROM claims.drug_ingredient_map WHERE insurance_code LIKE 'SMP-%';
DELETE FROM claims.dx_procedure_rules WHERE code LIKE 'SMP-%';
DELETE FROM claims.bundling_rules WHERE parent_code LIKE 'SMP-%';
-- surcharge_rates는 grade 자연키(SAMPLE 마커 없음) — 실값으로 덮어쓰거나 grade별 삭제.
```

## 거버넌스
- 룰 빈 상태 = 적발 0(무영향). 초기 severity는 WARN 운영 후 통계(거부사유·오탐률) 확인하고 BLOCK 승격.
- DUR 공개데이터 라이선스(유형2 비상업) 준수 — 상용 활용 전 식약처/심평원 확인.
- 적재 EP는 ADMIN 전용(`claims.rules write`). 임상 타당성 판단은 의료진 게이트(의료법).
