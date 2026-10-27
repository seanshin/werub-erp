# HIS 직원·의료진 등록 → ERP 동기화 인터페이스 요청서

> **목적**: HIS에서 직원·의료진 등록 기능이 준비 중 — 등록 데이터가 ERP `hr.employees`로
> 자동 동기화되도록 payload 스키마를 합의한다. (SoT 원칙 D-2: HIS=의료진·정규직 마스터,
> ERP는 읽기전용 미러 + ERP 전용정보(보수 프로필)만 보완입력)
>
> **상태**: 📨 HIS 팀 회신 대기 — 스키마 확정 시 `staff_sync.py` 패턴으로 consumer 구현
> **작성**: 2026-06-10 | **선례**: Clinic 직원 동기화(FR-28, `consume_clinic_staff`)

## 1. 요청 사항 (HIS 팀)

1. 직원 등록/수정/퇴사 시 ERP가 수신할 **payload 스키마** 확정 (아래 §2 제안 검토)
2. 전달 방식 선택: (a) Redis Stream `his.staff.changed` (기존 PO 패턴) (b) REST pull `GET /api/his/staff` (Clinic 패턴)
3. **상태 변경 이벤트**(퇴사·휴직·복직)도 포함 여부 — 미포함 시 ERP가 주기 full-sync로 대조

## 2. 제안 payload (Clinic 패턴 확장)

```json
{
  "id": "his-emp-uuid",            // 필수 — 멱등 키 (ERP external_user_id)
  "name": "김의사",                 // 필수
  "email": "doc@hospital.example.org",         // SSO JIT 계정 연결용 (강력 권장)
  "hisRole": "Doctor",             // Doctor|Nurse|Resident|LabTech|Radiologist|Admin|...
  "department": "내과",             // 부서명 (ERP가 이름으로 해소/생성)
  "position": "과장",               // 선택
  "licenseNumber": "MD-1001",      // 의료진 면허번호 (선택)
  "employmentType": "REGULAR",     // REGULAR|CONTRACT — HIS에 없으면 REGULAR 간주
  "joinedAt": "2026-01-02",        // 입사일
  "status": "ACTIVE",              // ACTIVE|RESIGNED|ON_LEAVE — 상태 동기화 핵심
  "resignedAt": null               // status=RESIGNED 시 필수
}
```

## 3. ERP 측 매핑 (확정분)

| HIS 필드 | ERP `hr.employees` | 비고 |
|----------|-------------------|------|
| `id` | `external_user_id` | 멱등 upsert 키 |
| — | `employee_no` | **H 접두 + 4자리** 자동 채번 (Clinic=C, ERP 직접=E) |
| `hisRole` | `job_group`/`job_category` | Clinic 매핑표 재사용 (Doctor→DOCTOR…) |
| `department` | `department_id` | 이름 해소, 미존재 시 생성 |
| `licenseNumber` | `hr.licenses` | license_no 기준 추가 |
| `status` | `resigned_on` 등 | RESIGNED→resigned_on 기록 (3년 보존 기산) |
| — | `source` | `HIS` 고정 — ERP에서 발령·수정 차단(SoT 가드 기존 구현) |

## 4. ERP 측 후속 처리 (구현 완료)

동기화된 직원은 **급여 기초정보 온보딩**(`GET /hr/onboarding/pending`, 웹 `/erp/hr/onboarding`)에
자동 표출 — 기본급·부양가족·급여계좌(암호화) 보완입력 후 급여 일괄생성
(`POST /hr/payrolls/{yyyymm}/generate-bulk`) 대상에 포함된다.

## 5. 미결 질문 (HIS 회신 필요)

- [ ] 주민등록번호는 전달하지 않는 것으로 가정 (4대보험 신고 시 별도 협의) — 동의?
- [ ] 휴직(ON_LEAVE) 상태를 HIS가 관리하는지, ERP가 관리해야 하는지
- [ ] 의료진 외 행정직도 HIS 등록 범위인지 (아니면 행정직은 ERP 직접 등록 유지)
- [ ] 기존 SSO JIT 사용자(`common.users`)와 직원 레코드 연결: email 기준 자동 매칭 동의?
