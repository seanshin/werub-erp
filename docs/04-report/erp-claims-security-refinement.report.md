# 사이클 완료 보고서 — 보안 + 사전심사 정밀화 + 아키텍처 계층화

> **Summary**: security-architect 점검 후속 보안 3건, 사전심사(claims pre-review) 룰 오탐·표시 정밀화 5건,
> 전면 아키텍처 계층화 1건, UI/가시성 개선 다수를 단일 세션에서 운영 배포·검증까지 완료. 모든 변경은
> claims 사전심사 트랙 + 공통 커널 정리로, **PR #188~#197 (10건) 전부 운영 LIVE + main 머지**.
> 테스트 503→512, 마이그 0056→0057. 핵심 결함은 "룰을 잘못된 카테고리에 적용한 오탐"(비급여·혈액제제·
> 약제 산정횟수)과 "경로 일관성 결함"(인보이스 vs 사전심사 매핑 불일치)으로, 단일 헬퍼·SoT 재사용으로 해소.
>
> **Project**: hospital-erp (claims 사전심사 트랙)
> **Cycle Type**: 보안 패치 + 룰 정밀화 + 아키텍처 계층화
> **Report Date**: 2026-06-14
> **Author**: weruby (Claude Code + bkit PDCA)
> **Status**: ✅ 완료 — 잔여는 외부 합류점(약제팀·HIS 회신 11건)뿐

---

## 1. 사이클 개요

| 항목 | 내용 |
|------|------|
| **시작점** | 세션 기록 검토 → security-architect 보안 점검 |
| **종료점** | 사전심사 진료일 가시성 일관화 + 전면 계층화 완료, 운영 검증 |
| **PR** | #188~#197 (10건) — 전부 squash 머지, 운영 LIVE |
| **테스트** | 503 → **512** (+9, 신규 회귀 테스트 다수) |
| **마이그** | 0056 → **0057** (PreReviewFinding.treated_on, nullable) |
| **배포** | rsync(coloc) → core+worker(+web) 재빌드 → 마이그 자동적용 → 재-sync 검증 |

---

## 2. PDCA 단계별

### Plan / Check (진단)
- **security-architect 점검**: OWASP 기준 잔여 취약점 도출 — HIGH 1(claims 변경 EP IDOR)·MED 2(patient 목록 dept·eapproval 콜백 재생).
- **운영 스크린샷 기반 진단**: 사용자가 명세 화면 스크린샷으로 오탐(미등재·비급여·혈액제제·약제 산정횟수)·표시 이슈(진료일 누락·날짜 오부착)를 짚음 → 운영 DB 조회로 근본원인 확정.

### Do / Act (수정)
PR #188~#197 — 아래 §3.

### 검증
- 단위/회귀 테스트 512 통과, ruff·tsc·eslint·lint-imports 클린.
- **운영 before/after**: 재-sync 후 DB로 적발 해소 실측(미등재 14→0 CONFIRMED·비급여 4→0·혈액제제 2→0·약제 산정횟수 34→0).
- 명세 API 직접 검증(진료일 부착 — 06-10 활성적발 0, 06-11에만).

---

## 3. 주요 산출물 (PR별)

| PR | 영역 | 내용 |
|----|------|------|
| **#188** | 보안 + 사전심사 | claims 변경 EP IDOR(HIGH 실해소)·patient 목록 dept·eapproval 콜백 재생방지 / 사전심사 약품매핑 미등재 오탐 해소(인보이스와 매핑 일관) |
| **#189** | UI | 사전심사 워크리스트 진료월별 탭 |
| **#190** | UI/가시성 | 명세 라인 진료일·적발 진료일 동반·목록 적발 BLOCK/WARN 분해 |
| **#191** | 룰 정밀화 | 비급여 라인 미등재(UNKNOWN_CODE) 제외 (D-1) |
| **#192** | UI 안전 | 약품 매핑 확정 — 후보 전체명칭 + 함량 불일치 경고 |
| **#193** | 룰 정밀화 | 혈액제제(약가 비대상) 미등재 제외 |
| **#194** | 아키텍처 | **전면 계층화** — common 순수 커널화 + hr↔finance 순환 해소(erp.app 레이어) |
| **#195** | 룰/표시 | 적발에 진료일(treated_on) — 날짜특정 적발 정확 부착 (마이그 0057) |
| **#196** | 룰 정밀화 | 약제·재료 일반 산정횟수 룰 제외 — 투여량 오탐 방지 |
| **#197** | UI | 케이스 검토 적발 테이블 진료일 컬럼 |

**문서**: `docs/01-plan/integrations/drug-map-confirmation-request.md` (약가 매핑 회신 요청서 — 약제팀·HIS·의료정보팀).

---

## 4. 보안 패치 상세 (#188)

| ID | 이슈 | OWASP | 조치 |
|----|------|-------|------|
| HIGH | claims 변경 EP IDOR — 부서 가드가 읽기 2곳만·게이트 no-op | A01 | 변경 6 EP에 dept 가드 + save_run dept_code 채움. **타 부서 적발 DISMISS(삭감 우회) 차단** |
| MED | patient 접수/예약 목록 dept 미필터 | A01 | DEPT 스코프 WHERE 합성 |
| MED | eapproval 콜백 재생방지 부재 | A08 | 서명 ts 신선도(±300s) + event_id Redis NX |

> 이전 "v1.6 IDOR 부서경계"가 읽기 경로만·게이트 no-op이던 **"고쳤다는 착시"**를 실해소.
> 운영 GRANT 경계·SSO aud/jti 등은 실서버/HIS 협의 항목으로 분리(미수정).

---

## 5. 사전심사 오탐 — "룰 적용범위 정합" 패턴

같은 근본 패턴(룰이 잘못된 카테고리에 적용)을 단일 헬퍼·SoT 재사용으로 일관 해소:

| 오탐 | 원인 | 조치 | 운영 결과 |
|------|------|------|----------|
| 미등재(매핑된 코드) | 인보이스는 매핑 적용·사전심사는 미적용 | `resolve_fee_with_mapping` 단일화(현행 폴백 포함) | CONFIRMED 14→0 |
| 비급여 미등재 | 급여 삭감룰을 비급여에 적용 | `_BENEFIT_ONLY_RULES`에 UNKNOWN_CODE 편입 | 비급여 4→0 |
| 혈액제제 미등재 | 약가 비대상을 약가 룰로 평가 | `exempt_codes`(matched_name SoT 재사용) | 2→0 |
| 약제 산정횟수 | 투여량(정·바이알)을 행위 산정횟수로 | `_is_drug_material` 제외 | DRUG 34→0 |

> 급여(행위) 적발은 전부 유지(TARIFF 산정횟수 44건 등) — 진성 위험만 남김.

---

## 6. 아키텍처 계층화 (#194)

import-linter ratchet의 알려진 부채를 전면 해소, **erp.app 레이어**(크로스도메인 집계·오케스트레이션) 도입.

- **Phase 1**: admin_router·export_router(전 도메인 집계) → erp.common→erp.app. sso의 hr.Employee 모듈 import를 테이블 직접조회로 디커플. 계약 신설("common 순수 커널").
- **Phase 2**: 월마감(감가 finance + 퇴직충당 hr + 리스) 오케스트레이션 → erp.app(MonthEndService+엔드포인트, URL 보존). finance→hr 제거 → 순환 해소. finance 계약 강화.

최종 레이어: **erp.app > 도메인 > common·audit(커널)**. lint-imports 7계약 KEPT, 순수 리팩터(동작·URL 무변경).

---

## 7. 배운 점 (재사용 플레이북)

`memory/erp-claims-problem-playbook` 으로 영속화. 유사 문제 진단 순서:
1. **SoT 판정** — 데이터 주인(ERP/HIS/약제팀/심평원)? ERP 소관=즉시수정, 외부=회신 요청서.
2. **경로 일관성** — 같은 판단을 두 경로서 다르게? → 단일 헬퍼 추출(오탐의 대부분).
3. **룰 적용범위 명시** — 어떤 coverage/code_type에 적용? (비급여·혈액·약제 제외).
4. **신호 의미 보정** — 이 위험이 이 맥락서 의미 있나? false BLOCK 제거.
5. **자동화 안전경계** — 환자안전·청구정합성? 자동은 후보까지, 확정은 사람(약품 매핑).

실행 원칙: **즉시 vs 외부 분리 · 배포≠해소(재-sync 재평가) · 운영 before/after 검증 · 외부 갭은 요청서**.

**핵심 통찰**: *같은 개념을 두 경로에서 다르게 계산하면 언젠가 어긋난다* — 미등재 오탐의 뿌리가
인보이스↔사전심사 매핑 불일치였듯, 예방의 최선은 판단 로직의 단일 진실원(헬퍼) 통합.

---

## 8. 외부 합류점 (잔여 — ERP 측 종결)

| 항목 | 담당 | 회신 시 |
|------|------|---------|
| 약가 매핑 확정 7건(후보적합) + 1건(함량불일치) | 약제팀 | `/erp/regulatory/drug-map` 확정 → sync 자동 해소 |
| 보험코드 회신 3건(후보없음) | HIS팀 | 등재 후 sync 자동 해소 |
| (D그룹 혈액제제 2건) | — | ✅ ERP 처리 완료(#193) |

> 회신 요청서: `docs/01-plan/integrations/drug-map-confirmation-request.md`.
> ERP가 즉시 처리 가능했던 오탐(비급여·혈액제제·약제 산정·매핑)은 **전부 운영 해소**.

---

## 9. 품질 지표

| 지표 | 값 |
|------|----|
| 테스트 | 503 → **512** (100% 통과) |
| 신규 회귀 테스트 | IDOR·dept·콜백·매핑·비급여·혈액제제·약제산정·진료일부착 등 |
| lint-imports | 7계약 KEPT(common·finance 강화) |
| ruff / tsc / eslint | 클린 |
| 마이그레이션 | 0057(nullable — 무중단) |
| 운영 배포 | 전 PR LIVE, HIS 무영향, restarts=0 |

---

## 10. 최종 판정

**사이클: ✅ 완료** — 보안 3건 실해소, 사전심사 오탐 4종·표시 정밀화, 전면 계층화까지 운영 반영·검증.
ERP 단독 dev 백로그 소진. 잔여 11건은 외부(약제팀·HIS) 회신이 합류점이며, 회신 시 재-sync로 자동 해소.

**재개 지점**: 약제팀/HIS 회신 도착 → `/erp/regulatory/drug-map` 확정/등재 → sync.

---

## 관련 문서·메모리
- 회신 요청서: `docs/01-plan/integrations/drug-map-confirmation-request.md`
- 보안 패치: `memory/erp-security-patch-idor`
- 문제 해결 플레이북: `memory/erp-claims-problem-playbook`
- dev-flow 원칙: `memory/erp-dev-flow-principle`
- 운영 배포: `memory/erp-prod-deploy`

---

## 11. 버전 이력
| 버전 | 날짜 | 내용 |
|------|------|------|
| 1.0 | 2026-06-14 | 보안+사전심사 정밀화+계층화 사이클(PR #188~#197) 완료 보고 |
