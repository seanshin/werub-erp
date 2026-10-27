# HIS → ERP 급여 기초정보 연동 — 요청서 + ERP 회신

**배경**: HIS `/staff/payroll`에 직원 셀프서비스 급여 기초정보(계좌·주민번호·부양가족·서류) 도입. 급여
산정/지급은 **ERP SoT 유지**, HIS = 입력 surface · ERP = 처리. HIS팀 요청 7건과 ERP 회신·구현 상태.

**작성일** 2026-07-09 · **상태** ERP 1차 회신(R-1/R-4/R-5 구현·나머지 결정 제시).

---

## 요청·회신 매트릭스

| # | 우선 | 요청 | ERP 회신 / 상태 |
|---|------|------|------------------|
| **R-1** | 🔴 | 민감정보 보안 수신채널(계좌·주민번호) | **구현 LIVE** — `POST /integration/hr/payroll-identity`. X-Integration-Key + **HMAC 서명**(X-Signature/X-Timestamp·재생방지). 계좌·주민번호 **컬럼 암호화(AES-GCM)** 저장·응답 마스킹. **전송방향=HIS push→ERP**(webhook 동형). mTLS는 배포 nginx에서 선택 적용. |
| **R-2** | 🔴 | 필드 스펙 합의 | **회신(아래 §필드 스펙)** — employeeCode·residentId·bankName·**bankCode(표준)**·bankAccount·holder(예금주)·dependents. HIS는 이 필드만 수집(과수집 방지). 4대보험/세무 추가필드는 ERP 산정이 처리(HIS 미수집). |
| **R-3** | 🟡 | SoT/마스터 경계 | **안A 채택** — HIS=입력 surface, **ERP=처리 마스터**. 계좌·주민번호·부양가족은 HIS 입력→ERP 저장(수신 시 반영), **기본급은 ERP 소유(수신이 덮지 않음)**. 충돌 규칙: 최신 수신이 계좌/부양가족 갱신, 기본급은 ERP만. |
| **R-4** | 🟡 | 급여기초 리드백 API | **구현 LIVE** — `GET /integration/hr/payroll-identity/{employeeNumber}`. ERP 등록분 **마스킹**(계좌 ***tail·주민 YYMMDD-*******·bankCode·holder·dependents). 미등록 시 registered=false. |
| **R-5** | 🟡 | 인적공제 규칙/코드 | **구현 LIVE** — `GET /integration/hr/deduction-codes`. 코드표: SELF·SPOUSE·DEPENDENT·DISABLED·ELDERLY·WOMAN·SINGLE_PARENT(income)·CHILD_TAX(tax_credit). 금액은 소득세법 기본(운영 갱신). |
| **R-6** | 🟢 | 변경 반영 시점·승인 | **제시** — 수신 즉시 ERP 저장, **효력=차기 급여 생성분부터**(당월 확정 후 변경은 반영 안 됨). 계좌 변경은 감사기록. HR 승인 게이트는 옵션(현 무승인 즉시반영·승인 필요 시 별도 이벤트 스펙 후속). |
| **R-7** | 🟢 | 매핑키(확인만) | **확인** — 기존 `POST /integration/hr/staff/map`(erpEmployeeCode↔employeeNumber) 재사용. 신규 없음. |

---

## §필드 스펙 (R-2) — `POST /integration/hr/payroll-identity` 본문

```jsonc
{
  "employeeCode": "2026-0009",     // 필수 — ERP 직원 식별(=employeeNumber, staff/map 키)
  "residentId":   "900101-1234567",// 주민번호(YYMMDD-DDDDDDD) — ERP 컬럼암호화 저장
  "bankName":     "국민은행",
  "bankCode":     "004",           // 표준 은행코드(이체파일용) — 권장
  "bankAccount":  "123-45-6789",   // 계좌 — ERP 컬럼암호화 저장
  "holder":       "김직원",         // 예금주(본인일치 검증 참고)
  "dependents":   3                // 부양가족 수(본인 포함) — 미제공 시 기존 유지
}
```

- **헤더**: `X-Integration-Key`(정적키) + `X-Signature`(HMAC-SHA256, `timestamp.본문` 서명·bare hex 수용)
  + `X-Timestamp`(신선도·재생방지). 시크릿 = `INBOUND_WEBHOOK_SECRET`(없으면 정적키).
- **응답**: `{employeeNumber, accepted, accountMasked, residentMasked, dependents}` — 평문 미회신.
- **미제공 필드는 기존값 보존**(부분 갱신). base_pay 등 ERP 소유 필드는 이 EP로 변경 불가.
- **예금주 본인일치**: holder 저장만(자동 검증은 후속 — 실명확인 API 연계 시).

## 저장/보안

- 계좌·주민번호 = `hr.compensation_profiles.bank_account_enc`·`resident_id_enc`(AES-GCM·행 PK AAD·마이그 0169).
  평문 조회 EP 없음(이체파일 생성 시 내부 복호). 조회는 마스킹만.
- 접근은 hr.compensation read(ADMIN/DIRECTOR) + 접속기록. 서버간 수신은 `_SYSTEM_ACTOR` 감사.

## 활성화 절차

1. **HIS push 시크릿 공유** — `INBOUND_WEBHOOK_SECRET` 양측 설정(HMAC).
2. HIS에서 `ess.payrollRelay.enabled=true` → 직원 입력→ERP 전송 개시.
3. 그 전까지는 통장사본·신분증 서류 제출 + ERP 관리화면 개별/일괄 입력으로 운영.

## 2차 요청 (R-8~R-11) — 개인포털 정보 링크 노출

HIS 개인포털이 필요로 하나 미노출(404)이던 4종을 ERP가 노출. 전부 기존 `/integration/hr/*` +
X-Integration-Key 패턴.

| # | 항목 | EP | 상태 |
|---|------|----|------|
| **R-8** | 당직/근무표 | `GET /integration/hr/duties?employeeNumber=&from=&to=` | **LIVE** — 기간 배정(duty_on·shift) |
| **R-9** | 성과급 실적 상세 | `GET /integration/hr/performance?employeeNumber=&period=` | **LIVE** — 진료수익·순수입·승인성과급·계약형 |
| **R-10** | 재직/경력 증명서 | `GET /integration/hr/certificates` + `.../{id}/pdf?employeeNumber=` | **LIVE** — ⚠ 정본 통일 결정 대기(§결정) |
| **R-11** | 퇴직금 추정 | `GET /integration/hr/severance?employeeNumber=&as_of=` | **LIVE** — 평균임금×근속(추정) |

## §결정 (HIS 회신 대기)

- **R-1 활성화**: 계좌·주민번호 직접 입력→ERP 전송은 `INBOUND_WEBHOOK_SECRET`(HMAC) 양측 공유 후
  HIS `ess.payrollRelay.enabled=true`로 활성. 시크릿 미공유 시 서류 제출+ERP 관리화면 입력으로 운영.
- **R-10 증명서 정본**: HIS 자체 발급(/ess/certificates·진위코드)과 중복 — ERP 정본 통일 vs HIS 발급 유지
  결정 후 배선. ERP는 EP를 열어두었으니 HIS가 선택 가능.

## 잔여(HIS 결정 필요)

- R-3 충돌 세부(양쪽 동시수정)·R-6 HR 승인 게이트 도입 여부·mTLS 적용 여부(배포 인프라)·
  예금주 실명확인 연계·bankCode 표준표(금융결제원) 공유.
