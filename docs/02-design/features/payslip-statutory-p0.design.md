# 급여명세서 법정 부합 P0 — 상세 설계

> **범위**: [[payslip-statutory-and-medical]] 기획의 **P0(법정 부합)** 구현 설계. 임금명세서 필수 기재사항 중
> **정적 항목**(지급일·근로자 식별·구성항목 한글명·항목별 금액/총액 = 시행령 §27의2 1~4·6호)을 충족한다.
> **계산방법(5호)** 은 `calc_basis`가 필요해 **P1**로 분리 — P0 완료 시 "부분 부합"(구조 완비), P1이 계산식
> 표기로 완전 부합. **작성일** 2026-07-08 · **상태** 설계 · **선행** 없음(신규 컬럼·테이블만).

---

## 1. 현행 요약 (착수 기준)

- `my_payslip_pdf`(`hr/router.py:1423`) → `common/pdf_templates/payslip.html`. 컨텍스트는 `name·employee_no·
  org_name·lines(item_code,item_kind,amount)·합계·confirmed_at`.
- `PayrollLine`(`hr/models.py:182`) = `(item_code, item_kind, amount)`. 코드 집합(현재 사용):
  - EARNING: `BASE·DUTY·NIGHT·QUAL·WATCH·OT·UNPAID_LEAVE·INCENTIVE`
  - DEDUCTION: `NP·HI·LTC·EI·TAX·LOCALTAX`
- `Employee`에 **`birth_date`·`position`(직급)·`employment_type` 이미 존재** → P0는 컬럼 추가 없이 주입만.
- 라인 생성: `payroll_rules.build_payroll` → `employee_service`(PayrollLine 루프 영속) / `incentive_service.
  _add_payroll_line`(INCENTIVE 라인).

## 2. 데이터 모델 (P0)

```
hr.payroll_records  + paid_on: date | null          # 임금 지급일(법정 §27의2 2호)
                                                     # nullable: 과거 확정분 무백필. 확정 플로우가 채움.

hr.pay_item_catalog (신규)                           # 항목코드 마스터(SoT·표시)
    code          varchar(20)  PK                    # BASE·OT·NP…
    name          varchar(40)  not null              # 한글 항목명 "기본급"·"연장근로수당"
    name_en       varchar(60)  null                  # 영문(국제병원 영문명세·P3)
    kind          varchar(10)  not null              # EARNING | DEDUCTION
    category      varchar(20)  null                  # 기본급/수당/성과/법정공제/세금
    taxable       boolean      not null default true  # 과세 여부(비과세 표기·연말정산)
    nontax_limit  numeric(12,0) null                  # 비과세 한도(식대 월20만 등)·초과분 과세 분리(M4)
    display_order smallint     not null default 100  # 명세 표시순서
    is_medical    boolean      not null default false # 의료진 전용 항목(P2)
    CHECK kind IN ('EARNING','DEDUCTION')
```

- `PayrollLine.calc_basis`(JSONB)·`item_name` 스냅샷은 **P1**. P0는 렌더 시 카탈로그로 한글명 해석
  (미등록 코드는 원본코드 표시 — 정직 폴백).
- `payment_method`는 계좌 PII 표기 이슈로 P0 제외(요약 "지급방법: 계좌이체" 정적 문구만 옵션).

### 마이그레이션

- alembic 리비전 1건: `create table hr.pay_item_catalog` + `add column hr.payroll_records.paid_on`.
- **시드(멱등 upsert)**: 카탈로그 17행(아래)을 마이그 내 `op.bulk_insert` 대신 **idempotent loader**
  (`scripts/seed_pay_item_catalog.py`, `ON CONFLICT(code) DO UPDATE`)로 — 운영 중 명칭·순서 조정 재실행
  가능(rule_loader 패턴 동형). 마이그는 테이블·컬럼만.
- `payroll_records`는 append-only 아님(확정 후 수정 감사 존재) → nullable 컬럼 추가 안전. **백필 없음**
  (과거 확정분 `paid_on=NULL` → 렌더 "—"·확정일 병기). ⚠ `erp_app` UPDATE 권한 REVOKE 테이블 아님 확인
  ([[erp-prod-deploy]] narcotics_ledger 교훈 — payroll_records는 해당 없음).

### 카탈로그 시드 (초기 17행)

**현행 `build_payroll` 생성분(14)** + **법정 보강분 3(HOLIDAY·BONUS·MEAL)** — 보강 항목의 계산 규칙은
P1 `build_payroll` 확장으로 채운다(현재 미생성). 카탈로그(코드 마스터)는 P0에서 선등록.

| code | name | kind | category | taxable | order | 비고 |
|------|------|------|----------|---------|-------|------|
| BASE | 기본급 | EARNING | 기본급 | ✓ | 10 | |
| BONUS | 정기상여금 | EARNING | 상여 | ✓ | 15 | 통상임금 포함 대상(2024 대법·§P1) — 계산규칙 P1 |
| OT | 연장근로수당 | EARNING | 수당 | ✓ | 20 | 통상시급×1.5×시간 |
| HOLIDAY | 휴일근로수당 | EARNING | 수당 | ✓ | 21 | §56: 8h내 ×1.5·초과 ×2.0 — 계산규칙 P1 |
| NIGHT_PREMIUM | 야간근로가산 | EARNING | 수당 | ✓ | 22 | §56 법정: 통상시급×0.5×야간시간(교대수당과 분리·M2) |
| SHIFT | 교대수당 | EARNING | 수당 | ✓ | 23 | 임의수당(정액·현 NIGHT_HOURLY 단가) |
| DUTY | 당직수당 | EARNING | 수당 | ✓ | 24 | |
| QUAL | 자격수당 | EARNING | 수당 | ✓ | 25 | |
| WATCH | 감시단속수당 | EARNING | 수당 | ✓ | 26 | |
| MEAL | 식대 | EARNING | 비과세 | 부분 | 27 | 비과세 `nontax_limit`=200,000·초과분 과세(M4) |
| INCENTIVE | 성과급 | EARNING | 성과 | ✓ | 30 | is_medical(의료진)·별도 생성(§P2·H1~H3) |
| UNPAID_LEAVE | 무급휴가 공제 | EARNING | 조정 | ✓ | 40 | 통상일급 음수(통상임금 단일함수·M3) |
| NP | 국민연금 | DEDUCTION | 법정공제 | — | 50 | |
| HI | 건강보험 | DEDUCTION | 법정공제 | — | 51 | |
| LTC | 장기요양보험 | DEDUCTION | 법정공제 | — | 52 | |
| EI | 고용보험 | DEDUCTION | 법정공제 | — | 53 | |
| TAX | 소득세 | DEDUCTION | 세금 | — | 60 | |
| LOCALTAX | 지방소득세 | DEDUCTION | 세금 | — | 61 | |

> ⚠ 현 `payroll_rules.build_payroll`은 **HOLIDAY·BONUS·MEAL·NIGHT_PREMIUM(법정 야간가산)·SHIFT 미생성**
> (기존 코드 `NIGHT`은 정액 단가). 이들은 P1에서 계산규칙 추가(휴일/야간 §56 가산·상여 통상임금 반영·식대
> 한도 분리). 카탈로그는 코드·표시 SoT로 P0 선등록, 규칙 없는 항목은 라인 미생성(정직).
> **전환기**: 현행 `NIGHT` 코드 라인이 남아있으므로 카탈로그에 `NIGHT`(→ "교대수당") **별칭 1행을 함께
> 등록**(원본코드 노출 방지)하고, P1에서 NIGHT_PREMIUM/SHIFT 분리 시 마이그레이션. (∴ 초기 시드 실제
> 18행 = 17 + 레거시 NIGHT 별칭)

## 3. API / 서비스 변경

`my_payslip_pdf` (그리고 관리자 개별/일괄 발행 시 공용 헬퍼로 추출):

```python
cat = {c.code: c for c in await PayItemCatalog.all(session)}   # 코드→카탈로그
ctx = {
  "org_name": await org_display_name(session),
  "employee": {"name": emp.name, "employee_no": emp.employee_no,
               "birth_date": emp.birth_date, "position": emp.position},
  "yyyymm": yyyymm, "paid_on": record.paid_on, "confirmed_at": record.confirmed_at,
  # 시행령 §27의2 단서 — 두 특례는 대상·범위가 다른 별개(둘 다 "생략 가능"=임의):
  "omit_identity": emp.employment_type == "DAILY",   # ① 생년월일·사번 등 특정정보(일용)
  "omit_calc":     emp.employment_type in ("DAILY", "WATCH"),  # ② 계산방법(일용·근로시간 적용제외=감시단속)
  "earnings": _rows(lines, "EARNING", cat),          # {name, amount, taxable} 표시순 정렬
  "deductions": _rows(lines, "DEDUCTION", cat),
  "total_earning": …, "total_deduction": …, "net": …,
}
```

- `_rows`: 카탈로그로 `item_name` 해석(미등록=코드)·`display_order` 정렬·`taxable` 부여.
- **고용형태 특례 분기(M6)**: `omit_identity`(일용)=생년월일·사번 생략 / `omit_calc`(일용·감시단속 WATCH)=
  계산식 생략. 둘은 **별개 특례**이고 법상 **임의**("생략할 수 있다") — 기본은 전체 표기, 특례는 기관 정책
  옵션으로 켠다(강제 드롭 아님).
- 확정 플로우(`confirm_payroll`, `hr/router.py:1186`)에 **`paid_on` 파라미터 추가**(지급일 확정 입력).
  미입력 허용(경고)·추후 정정 가능.

## 4. payslip.html 개편

```
┌ {org_name} ─────────────────  임 금 명 세 서 ─────────────────┐
│ 대상: {yyyymm}      지급일: {paid_on|—}      발행: {confirmed_at} │
│ 성명 {name}  생년월일 {birth_date}  사번 {employee_no}  직급 {position} │   ← 일용은 생년월일/직급 생략
├─────────────── 지급 내역 ───────────────┬──── 공제 내역 ────┤
│ 항목            금액(원)   과세          │ 항목        금액(원) │
│ 기본급         2,000,000                 │ 국민연금     90,000 │
│ 연장근로수당     143,000                 │ 건강보험     70,900 │
│ 식대(비과세)     100,000   비과세         │ 소득세       31,000 │
│ …                                       │ …                  │
│ 지급 합계      2,243,000                 │ 공제 합계   250,000 │
├──────────────────────────────────────────────────────────────┤
│                          실수령액   1,993,000 원                │
└──────────────────────────────────────────────────────────────┘
※ 본 명세는 {confirmed_at} 확정본입니다. (P1: 연장·야간 등 계산식 표기)
```

- 지급/공제 **2열 레이아웃**·표시순 정렬·비과세 배지·실수령 강조.
- P1에서 각 라인 아래 `calc_basis` 계산식 행 삽입(예 "10h × 9,569 × 1.5")을 위한 자리 확보(빈 렌더 무해).

## 5. 하위호환 / 마이그레이션 안전

- 과거 확정 명세: `paid_on=NULL` → "지급일 —(확정 {confirmed_at})" 표기. 허위 없음.
- 카탈로그 미등록 코드: 원본코드 그대로(신규 수당 도입 시 시드만 추가).
- API 응답(`my_payroll` JSON): `item_name` 필드 **추가**(기존 필드 유지 — 비파괴). 웹 화면 병행 반영.
- 롤백: 컬럼·테이블 drop만(데이터 소실=신규분만). 템플릿은 이전 버전 병존 가능.

## 6. 테스트

- 카탈로그 해석: 코드→한글명·표시순·비과세 배지(미등록 코드 폴백).
- 지급일: `paid_on` 설정 시 표기·미설정 시 "—"·확정일 병기.
- 고용형태 분기: DAILY=간이(생년월일/직급 생략)·REGULAR=전체.
- 합계 정합: earnings/deductions/net = 라인 집계와 일치(회계 전표 대사는 P1/§8).
- 렌더 스모크: PDF 생성 200·본인만 접근(기존 스코프)·PRINT 접속기록.

## 7. 산출 / 완료 기준

- [ ] `hr.pay_item_catalog` 생성·17행 시드(멱등 로더)·`payroll_records.paid_on` 추가(마이그 1건).
- [ ] 명세서에 지급일·생년월일·직급·**항목 한글명**·과세구분·표시순 반영.
- [ ] 고용형태(일용/정규) 필수항목 분기.
- [ ] 확정 플로우 지급일 입력·과거분 무백필 안전 표기.
- [ ] 테스트 통과·import-linter 유지(카탈로그는 hr 내부·common 역류 없음).

**P1 예고**: `PayrollLine.calc_basis`(+`item_name` 스냅샷) 추가 → `build_payroll`·`statutory`·인센티브
엔진이 근거 채움 → 계산식 행·공제 산정근거로 **계산방법(5호) 완전 부합**.
