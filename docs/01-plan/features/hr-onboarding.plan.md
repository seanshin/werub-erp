# Plan — 급여 기초정보 마스터 + 온보딩 보완입력 (hr-onboarding)

> **배경**: HIS에서 직원·의료진 등록 준비 중. 인사 기능 검토(2026-06-10) 결과
> P0 공백 2건 — ① 급여 generate가 기본급·부양가족을 매월 body로 받는 비영속 구조,
> ② HIS/Clinic 동기화 직원의 ERP 보완입력 절차 부재.
>
> **레벨**: Enterprise | **상태**: ✅ 구현 완료 (단일 사이클)

## 범위

| # | 항목 | 내용 |
|---|------|------|
| 1 | 보수 프로필 마스터 | `hr.compensation_profiles` (기본급·부양가족·계좌 AES-GCM 암호화, 마이그 0033) |
| 2 | 온보딩 체크리스트 | `GET /hr/onboarding/pending` — 재직자 누락항목 식별(기본급/계좌/계약만료일/일당) |
| 3 | generate 폴백 | 월별 입력 미제공 시 프로필 기본값, 미등록이면 온보딩 유도 오류 |
| 4 | 일괄 생성 | `POST /hr/payrolls/{yyyymm}/generate-bulk` — 프로필 기반, 확정분·일용직 스킵 |
| 5 | 웹 화면 | `/erp/hr/onboarding` — 대기 목록 + 보완입력 폼 (FIN 권한) |
| 6 | HIS 인터페이스 요청서 | `docs/02-design/features/his-staff-sync.interface.md` — HIS 팀 회신 대기 |

## 비범위 (후속 P1·P2)

휴직/복직, 근태→급여 자동산정(OT), 4대보험 취득·상실 신고 추적, 오프보딩 워크플로,
임금테이블 DB화, 법정의무교육·건강검진, 간호등급 모니터링.

## 보안

- 계좌번호 컬럼 암호화(AAD=PK 바인딩, design §6.4), 응답은 마스킹(`***1234`)만 — 평문 조회 API 없음
- 전용 정책 `hr.compensation`: read=DIRECTOR/ADMIN, write=ADMIN (급여 SELF 조회보다 좁음)
- upsert 감사기록(audit), 라우트 스모크 등록(이중 prefix 회귀 방지)
