# ERP v1.6.1 릴리스 — 자격 본인부담 SoT + v1.6 점검

> **Summary**: v1.6.0 직후(2026-06-12) **PR #184~#187, 4건**. HIS 핸드오프 대응과 자체 점검이 두 축.
> ① **자격 본인부담 SoT 10/10**: 의료급여 1·2종·산정특례·자격상실·차상위·보훈·산재·자보·자비·일반
> ② **HIS 자격 정밀화 수신**: insuranceProductCode→MASTER 직행·itemCategory·copayCategory
> ③ **v1.6 품질·보안 점검**: Critical 2·High 4 수정
>
> **테스트**: 498 → **503** | **마이그레이션**: 0053 → **0056** | README **v1.6.1** | 전 PR CI 그린·무중단

---

## 1. v1.6 품질·보안 점검 (#184)

대규모 v1.6 변경(25 PR) 직후 code-analyzer·security-architect 병렬 점검:
- **C1 보훈 PATRIOT**: coverage가 DB CHECK 위반 — 보훈 환자 라인 인입 실패·엔진 분기 사문화 → CHECK 확장
- **C2 매핑 약품 사전심사**: price_source=MAP 약품의 약가상한·비급여청구 BLOCK 침묵 → 보험코드 fee 노출
- **보안 IDOR**: claims 협업 EP 부서경계 미적용 → DEPT 스코프(상세·명세)
- **품질**: 종별 진료월 시점·confirm 제형 검증·접속기록·confirmed limit cap

## 2. HIS 자격 정밀화 수신 (#185)

HIS가 `billing.line_recorded`에 자격 필드 동봉 → ERP 수신·적용(마이그 0055):
- **insuranceProductCode**(자격검증 보험코드) → 단가 **최우선 MASTER 직행** — 약제팀 확정 없이도 보험코드 단가(제네릭 보류건 자동 해소). 단가 우선순위: HIS 보험코드 → ERP 마스터 → 매핑(MAP) → HIS 폴백
- itemCategory·eligibilityStatus·copayRate 수신
- 직원 34건 HR 반영, P1(백필/webhook payload 일치) 검증

## 3. 자격 본인부담 SoT — 10/10 완결 (#186·#187)

HIS와 **케이스 분리 SoT** 합의 → split_line 구현 → 시드 매트릭스 검증:

| 자격 | 본인부담 | SoT |
|------|:-------:|-----|
| 의료급여 1종 | 0원 | HIS copayRate |
| 의료급여 2종 | 15% | HIS copayRate |
| 산정특례 | 5% | HIS copayRate |
| 자격상실 | 전액 | HIS copayRate |
| 차상위(NEAR_POOR) | 14% | HIS copayRate |
| 보훈(PATRIOT) | 0원(국비) | coverage 분기 |
| 산재·자보 | 0원 | coverage 분기 |
| 자비 | 전액 | coverage 분기 |
| 일반 건보 | ERP 종별(병원 40%) | ERP 자체계산 |

```
일반 급여 → ERP 자체계산(기관 종별·encounter)
자격 특수 → HIS copayRate(공단 자격조회 SoT)
  ※ payload 신호(medicalAidClass·specialCaseType·eligibilityStatus·copayCategory)로 경계 자명
```

발견·수정 2건:
- **보훈 인입 실패**: 0040 인라인 CHECK 자동명(`billing_lines_coverage_check`) 잔존(0054는 ck_bl_coverage만 — 0046 동일 패턴) → 자동명 제거(0056)
- **차상위**: copayCategory 신호 ERP 미수신 → 수신·SoT 규칙 추가, HIS 백필 동봉 → 14% 검증

## 4. 협업 — ERP triage 정확성

- **UUID 'P' 오류**: ERP가 "ERP→HIS 호출엔 UUID 파라미터 없음 → HIS 내부 결함"으로 triage → HIS TwinController `resolvePatientId` 수정 확인
- 회신서 8종 왕복(handoff·SoT 합의·시드 검증·매트릭스·종결)으로 자격 트랙 양방향 종결

## 5. 남은 합류점 (외부·운영)
- 제네릭 실제품 보험코드(약제팀 — HIS drug-substitution·drug-master 도구 제공)
- 실물 MEDRLT(HIRA 운영 후) · Clinic W.Sign·sign P1(mTLS/HSM 게이트 해제)

---
**작성**: Claude Code (bkit PDCA) · 2026-06-12 · PR #184~#187 · 테스트 503 · 마이그 0056 · README v1.6.1
