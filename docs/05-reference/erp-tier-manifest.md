# ERP tier 매니페스트 (N2 — PHI-bound / location-free 분류)

> ARCHITECTURE-INVARIANTS N2 원칙: 각 모듈을 PHI-bound(병원 로컬 필수)와 location-free(배치 자유)로
> 분류·유지한다. location-free 모듈은 PHI 스키마 직접 참조 금지.
> **현행 배포 사실**: ERP 전 서비스는 병원 서버 로컬(HIS 동거·클라우드 tier 없음) — 본 매니페스트는
> 장래 tier 분리의 기준이며, location-free 분류가 곧 클라우드 배치를 의미하지 않는다.
> 갱신 규칙: 모듈에 환자 식별자 컬럼/참조를 추가하는 PR은 본 문서를 함께 갱신할 것.

## 배포 단위

| 서비스 | tier | 근거 |
|---|---|---|
| core (API 전체) | **PHI-bound** | patient/claims 등 PHI 도메인 포함 단일 프로세스 |
| worker-integration | **PHI-bound** | 인바운드 소비(chartNo 등) 코드 공유 |
| worker-batch | **PHI-bound** | pg_dump 백업(PHI 전체)·환자 배치 |
| web (Next.js) | **location-free** | 정적 셸만 — SSR/RSC PHI 접촉 0(감사 INV-PHI-02 PASS) |
| Grafana/Metabase | **location-free** | reporting 집계 마트 전용(erp_bi 읽기전용·환자 단위 행 없음) |

## 모듈 분류

| 모듈 | 분류 | 근거 |
|---|---|---|
| erp.patient | **PHI-bound** | chart_no·성명·생년월일·phone_enc·BillingLine(약품·상병)·VOC |
| erp.claims | **PHI-bound** | chart_no·상병·사전심사 워크리스트(차트×월) |
| erp.integrations | **PHI-bound** | HIS 인바운드 chartNo·약품·상병 저장. 아웃바운드 AI는 filter_gate 비식별 |
| erp.regulatory | **PHI-bound(결합)** | 마스터 자체는 자유이나 filter_gate가 Patient 참조(실데이터 대조) |
| erp.tax | **PHI-bound(부분)** ⚠ | 연말정산 의료비 EP만 환자 chart+실명 — 이관 시 location-free화 가능(사람 결정) |
| erp.hr | **직원PII**(PHI 아님) | 주민번호 AES-GCM·급여. 환자 참조 없음 |
| erp.finance | **location-free(코드)** ⚠ | 도메인 import 금지 계약·모델에 환자 컬럼 없음. 단 전표 memo에 chart_no 유입(경계 누수 — patientRef 전환 시 해소) |
| erp.scm | **location-free(대부분)** ⚠ | 위탁재고 사용 chart_no 1필드가 유일 누수 |
| erp.costing / erp.itam | **location-free** | 환자 참조 없음(계약·grep 확증) |
| erp.analytics(reporting) | **location-free(집계)** | 월·부서 집계만·환자 카운트 int뿐 |
| erp.audit | **경계 유의** | target_id에 chart_no 참조 가능 — patientRef 도입 시 일괄 결정 |
| erp.common / erp.app | 커널 — core와 함께 이동 | |

## 경계 누수(장래 분리 선행 과제)

1. finance 전표 memo의 chart_no (integrations 기록분)
2. scm 위탁재고 `chart_no` 컬럼
3. tax 연말정산 환자 명세 EP

→ 셋 다 HIS patientRef(가명 토큰) 전환과 함께 치환하는 것이 효율적(회신 B 참조).
