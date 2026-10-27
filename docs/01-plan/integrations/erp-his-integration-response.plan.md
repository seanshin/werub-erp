# ERP ↔ HIS 연동 — ERP 측 대응 계획서 (HIS 마스터플랜 v1.0 응답)

> **대상**: HIS 「HIS↔ERP 연동 마스터 계획서 v1.0」(2026-06-09)에 대한 ERP 측 응답.
> **목적**: HIS가 도출한 도메인·Wave·결정사항(D-1~D-5)에 대해 **ERP 현황 매핑 + 입장 + 정합점**을 정리.
> **결론 한 줄**: **ERP는 재무·자재·자산·구매 도메인의 수신 측 모델·로직을 이미 보유** —
> Wave별로 **수신 어댑터(웹훅 수신 + 멱등 적재)** 추가만으로 대부분 연동 가능.
> **작성일**: 2026-06-09 · **상태**: 응답 · **연관**: HIS `erp-integration-master.plan.md`
>
> 📌 **범위**: 본 문서는 HIS 마스터플랜(전략)에 대한 **마스터 층위 응답** — 이 1건으로 결정(D-1~D-5)·
> Wave·현황 회신이 가능합니다. **도메인별 상세 인터페이스는 별도 명세서**로 산출:
> - 향후 자재/재무/자산 Wave: 도메인별 spec(HIS `cssd-erp-integration-spec` 패턴 대응)

---

## 1. ERP 현황 매핑 — HIS 도메인별 준비 상태

표기: 🟢 ERP 모델·로직 보유(수신 어댑터만) · 🟡 부분 보유 · 🔴 신규

| HIS 도메인 | HIS→ERP 데이터 | ERP 현재 보유 | 준비 |
|-----------|----------------|---------------|:--:|
| **billing/claim** 수납·청구 | 매출·미수·일마감 | `VoucherService`·**일마감→전표 체인**·`SettlementService`(AR/AP)·claims(삭감) — **HIS 환자/수납 수신 어댑터(`his.py`)로 이미 일부 수신** | 🟢 |
| non-covered 비급여 | 매출 | 전표(수익 계정) | 🟢 |
| cost-analysis 원가 | 원가·수익성 | `costing`/`abc_service`(ABC 배부)·`statements` | 🟢 |
| **inventory/pharmacy** 자재 | 재고·입출고 | `scm`(Item·Lot·FIFO·StockMovement·NarcoticsLedger) | 🟢 |
| blood-bank/lab/nutrition 자재 | 재고 | 동일 scm 패턴(category 확장) | 🟡 |
| **equipment** 의료기기 | 자산·감가·A/S | `FixedAsset`·`DepreciationEntry`(취득·정액/정률 감가·처분) | 🟢 |
| 디바이스 IT자산 | 자산·배치 | FixedAsset(category 확장) | 🟡 |
| **Supplier** 공급업체 | 거래처 공통화 | `scm.Supplier` 보유 — **거래처코드 매핑 필요** | 🟡 |
| **PO** 발주·수령 | 구매요청·입고 | `PurchaseOrder`·`PoLine`·**procure-to-pay(입고→미지급금)** — ERP가 SoT | 🟢 |
| **hr/staff** 인사 | 인사·부서·스케줄 | `Employee`(Clinic 동기화)·`Appointment` — **SoT 정합 필요(§3 D-2)** | 🟡 |
| staff 근태 | 근태→급여 | `AttendanceSummary`·**groupware 근태 수신 어댑터 보유**·`PayrollRecord`·법정처리 | 🟢 |
| staff-qualification 자격 | 면허 | Employee 면허·만료알림 | 🟢 |

> **핵심**: HIS 마스터플랜의 거의 모든 수신 측을 ERP가 이미 갖췄습니다(이번 분기 재무·AR/AP·자산·
> 원가·procure-to-pay 구축 완료). 신규는 대부분 **"HIS 웹훅을 받아 staging→멱등 적재"** 어댑터입니다.

---

## 2. 연동 패턴 정합 (HIS 표준 ↔ ERP 기존)

| 요소 | HIS 마스터플랜 | ERP 현재 | 정합안 |
|------|----------------|----------|--------|
| 인증 | `X-Integration-Key`(도메인별) | `Bearer`+`X-Hospital-Code`(Clinic), 도메인별 키 가능 | **ERP가 `X-Integration-Key` 수용** — 도메인별 키 발급 |
| 인바운드(ERP→HIS) | `POST /api/v1/integration/{domain}` | ERP→HIS REST(his.py) | HIS 규격 채택 |
| **아웃바운드 웹훅**(HIS→ERP) | `*.changed` POST | **ERP staging 테이블 패턴**(`integration.*_staging` + 멱등 consume) | ERP가 **웹훅 수신 엔드포인트** 제공 → staging 적재 → 멱등 처리 |
| 신뢰성 | outbox 재시도(D-5) | 멱등 `source_ref` + staging 격리·재처리 | HIS outbox ↔ ERP **멱등 inbox**(source_ref UNIQUE) = 정확히 정합 |
| 코드 매핑 | `erpItemCode`/`erpRefCode` | external_user_id 등 외부키 패턴 보유 | 도메인별 매핑 컬럼 추가 |
| 설정 | `/admin/erp` | ERP `/admin` 연동설정(신규) | 양측 관리화면 |

> HIS의 **outbox→웹훅**과 ERP의 **staging→멱등 consume**은 서로의 짝(전송보장 ↔ 수신멱등)이라
> 신뢰성 모델이 자연 정합됩니다.

---

## 3. 결정사항(D-1~D-5) — ERP 입장

| # | HIS 기본안 | **ERP 입장** |
|---|-----------|--------------|
| **D-1** 연동키 | 도메인별 | ✅ **동의** — ERP ACL 어댑터는 도메인별 키 분리 가능(현 clinic/his/fee 별도 키와 동일 철학) |
| **D-2** 인사 SoT | **ERP** (HIS는 근태) | 🟡 **정합 필요** — 현재 직원은 **Clinic→ERP** 단방향 동기화(`external_user_id`). "ERP=SoT"로 가면 방향 전환 필요. **제안: 디렉터리(계정·로그인) SoT=Clinic/HIS, 인사관리·급여·발령 SoT=ERP**로 역할분리(현 흐름 유지하며 ERP가 인사 마스터). 협의 항목 |
| **D-3** 재무 전표 | 일마감 배치+정정 실시간 | ✅ **이미 구현** — ERP `daily_closing→VoucherService` 전표, **FR-54 정정=역분개+재마감**. 완전 일치 |
| **D-4** inventory vs cssd | 역할분리(범용 vs 멸균품) | ✅ **동의** — ERP `scm.items.category`(DRUG/SUPPLY/EQUIPMENT)로 범용 자재 수용, 멸균품은 별 분류/연동 |
| **D-5** 웹훅 신뢰성 | W1 outbox 도입 | ✅ **동의** — ERP는 수신 측 멱등(`source_ref` UNIQUE)+staging 재처리로 대응. HIS outbox와 1:1 |

---

## 4. Wave 로드맵 — ERP 대응

| Wave | HIS 범위 | ERP 대응(수신 측) | ERP 준비도 |
|------|---------|-------------------|:--:|
| **W0**(완료) | CSSD 재고 | — | — |
| **W1** | 공급업체 마스터 + inventory + pharmacy | **거래처코드 매핑** + scm 재고 웹훅 수신 어댑터(입출고→Lot/StockMovement 멱등 적재) | 🟢 모델 보유 |
| **W2** | billing/claim→회계 + equipment | billing 웹훅→**전표/AR**(기존 his.py 확장) · equipment→**FixedAsset** 등록·감가 | 🟢 모델·로직 보유 |
| **W3** | hr/근태→급여 + 잔여자재 + 디바이스 | 근태 웹훅→`AttendanceSummary`→**급여 산정**(기존 groupware consume 확장) · 자재 category 확장 | 🟢 보유 |
| **(병행)** | 전자결재(HR) | **HIS W.Sign 릴레이**(별도 ADR/요청서) | 🟡 요청 중 |

> **선행(W1 이전) 정합 2건**:
> 1. **거래처(Vendor) 마스터 통합** — HIS가 공통 Vendor 승격 시, ERP `scm.Supplier`에 `erp_vendor_code`
>    매핑(전 자재·구매 도메인 공유 기준).
> 2. **코드 매핑 규약** — HIS 코드 ↔ ERP 코드(품목·계정·부서·거래처) 매핑표 양식 합의.

---

## 5. ERP 측 선반영 가능 항목 (HIS 회신 전)

- **웹훅 수신 게이트웨이**: `POST /api/v1/integrations/his/{domain}/webhook` (X-Integration-Key 검증 →
  도메인 staging 적재 → 멱등 consume). 기존 ACL 어댑터·staging 패턴 재사용.
- **거래처/코드 매핑 컬럼**: `scm.Supplier.erp_vendor_code` 등 외부키 컬럼.
- **연동 관리화면**: `/admin` 도메인별 연동 on/off·키·웹훅·모니터.
- 재무 전표(D-3)·자산 감가·AR/AP·procure-to-pay·근태→급여는 **이미 동작** — 수신 매핑만 연결.

---

## 6. ERP → HIS 회신 요청

1. **D-2 인사 SoT** 역할분리(디렉터리=Clinic/HIS, 인사관리·급여=ERP) 수용 여부.
2. **거래처(Vendor) 마스터 통합** 시점·코드 체계(W1 선행).
3. **웹훅 규격 확정** — `X-Integration-Key` 발급(도메인별), `*.changed` payload 스키마, 재시도 정책.
4. **코드 매핑표 양식**(품목·계정·부서·거래처).
5. **billing/청구 → ERP** payload — 기존 ERP `his.py`(환자·수납 수신)와의 정합(일마감 단위 vs 건별).
6. Wave별 도메인 명세서(`cssd-erp-integration-spec` 패턴) ERP 전달 일정.

---

## 7. 결론

- HIS 마스터플랜의 **수신 측 ERP 모델·로직은 이번 분기 구축으로 대부분 완비**(재무·AR/AP·자산·원가·
  SCM·procure-to-pay·근태→급여). → Wave는 **웹훅 수신 어댑터 + 코드/거래처 매핑** 중심으로 빠르게 진행 가능.
- 결정 **D-3·D-4·D-5는 ERP 현 구조와 일치**, **D-1 동의**, **D-2(인사 SoT)만 역할분리 협의** 필요.
- 다음: HIS와 **W1 선행(거래처·코드 매핑) + 웹훅 규격** 확정 → ERP 수신 게이트웨이 구현.
