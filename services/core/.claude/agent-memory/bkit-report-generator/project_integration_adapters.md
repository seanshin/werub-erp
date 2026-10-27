---
name: project-integration-adapters-m0a
description: 병원 ERP 외부 연동 ACL 어댑터 2종(HIS·그룹웨어) 구현 완료 — M0a=REST 확정, Match Rate 96%
metadata:
  type: project
---

## 외부 연동 ACL 어댑터 2종 PDCA 완료

**완료 날짜**: 2026-06-08

### 핵심 성과

- **RestHISPatientSource** + **RestGroupwareAttendanceSource** 실 구현
- M0a(HIS 연계 방식) 확정: **REST API** (공개 문서 검색으로 확정)
- 멱등 스테이징(source_ref UNIQUE) + 기존 서비스 재사용으로 FR-54 불변식 자동 승계
- Match Rate **96%** (120/125 가중점), 단위 테스트 **190개**(+16), E2E PostgreSQL 검증 완료
- Critical gap **0**

### 구현된 어댑터

1. **그룹웨어 근태** (`integrations/groupware.py`)
   - Protocol: GroupwareAttendanceSource
   - 호출: `GET /api/v1/clinic/attendance?since={datetime}`
   - Collector: 멱등 적재 → integration.groupware_attendance_staging
   - 소비: job_attendance_summary(04:00) → hr.attendance_summaries 월 집계
   - 테스트 6개

2. **HIS 환자·수납** (`integrations/his.py`)
   - Protocol: HISPatientSource (환자 + 수납 2개 메서드)
   - 호출: `GET /api/v1/erp/patients`, `GET /api/v1/erp/payments`
   - Collector: 2종 멱등 → integration.his_{patient,payment}_staging
   - 소비: 실시간/배치(job_his_sync, 10분) → patient upsert + reception + voucher(F1 흐름)
   - phone AES-GCM 암호화 + staging 파기(H-2 수정)
   - 테스트 10개

### 마이그레이션

- `0005_groupware_attendance.py`: integration.groupware_attendance_staging 생성
- `0006_his_patient_payment.py`: integration.{his_patient,his_payment}_staging 생성

### 보안 패턴

공통: SSRF 화이트리스트(clinic.example.org, his.example.org) + TLS verify 강제 + Bearer + X-Hospital-Code 인증

### M0a 확정 경과

- 초기: hospital-erp.prd에서 "M0a 구현 후 확정"이라고만 기재
- 조사: 공개 문서(Hospital RUN 기술 문서, example.org) 검색 → REST 스펙 발견
- 결론: M0a=**REST API** ✅
- 계약 미확정 상황에서도 MockTransport로 테스트 가능 → 계약 후 어댑터만 교체

### 배운 점

1. **설계 우선의 이점**: Protocol 사전 정의 → 계약 미확정 상황에서도 어댑터 구현 가능
2. **기존 서비스 재사용**: PaymentService 호출로 FR-54(마감 후 수납 거부) 자동 승계 — DRY 원칙
3. **공개 문서 조사**: "외부 의존"으로 보였던 M0a가 공개 표준 검색으로 확정 → 선행 조사의 가치
4. **민감정보 처리**: staging phone 평문 저장 → 소비 후 NULL 파기 (gap H-2 수정)

### 미완 항목

- **H-1**: 근태 지각/조퇴 산정(late_minutes, early_leave_minutes) — hr.duty_assignments 통합 필요(Phase 2)
- **M-1**: FAILED reprocess API — 운영 필요 시 패치
- **M-2**: day_closed FAILED 운영 문서 — 작성 필요
- **H-3/H-4**: 설계 문서 갱신(§3.5, §5.1) — v0.2 개정

### 외부 의존

- **HIS ERP-sync 엔드포인트**: 미확정(문서 발견, 정식 계약 미체결) → 2026-06-15 예상

### 보고서 위치

`docs/04-report/integration-adapters.report.md` (한국어, 기존 병원-erp.report.md와 별도 파일)

### 다음 단계

1. M0a 계약 서명 → 엔드포인트 확정
2. 운영 환경 배포: docker-compose 환경변수화, 스케줄러 활성화
3. HIS 검증: mock → 실 엔드포인트
4. Phase 2: 나머지 3종 어댑터(Hira, FeeMaster, FileDropAdapter) 구현
