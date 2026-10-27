# BI 확장 후보 점검 — 경영 BI(Grafana)·셀프서비스 BI(Metabase) 각 10종

> **목적**: 두 BI 도구 각각 추가 구현 가능한 항목이 충분한지(≥10종) 점검. 결론: **둘 다 10종+ 구현 가능**.
> 공통 인에이블러 = **reporting 마트 확장**(현 3종 → +7종). analytics·finance·scm 집계 메서드가 이미 있어
> 마트화는 기존 패턴(`analytics/marts.py` refresh_* + 야간 잡) 반복.
> **작성일**: 2026-06-30 · **상태**: 점검(미구현) · 연관 [[bi-tool-integration]]·`erp-bi-tool-integration`(memory).

## 0. 공통 선행 — reporting 마트 확장(현 3 → 10)
현 마트: `mart_exec_kpi_monthly`·`mart_dept_pl_monthly`·`mart_audit_flags_monthly`. 추가 후보(집계 메서드 존재):
| 신규 마트 | 소스 메서드 | 비고 |
|---|---|---|
| `mart_cashflow_monthly` | settlement.cash_flow / cash_flow_statement | 영업·투자·재무 현금흐름 |
| `mart_ar_ap_aging` | settlement.aging(AR/AP) | 연령버킷별 채권·채무 |
| `mart_cash_forecast` | settlement.cash_forecast | 자금수지 예측(6개월) |
| `mart_inventory_monthly` | scm.valuation·turnover·shortage | 재고자산·회전율·안전재고미달 |
| `mart_income_lines_monthly` | statements.income_statement(by_function/account) | 계정/기능별 수익·비용 |
| `mart_ratios_annual` | statements.financial_ratios | 유동·부채·이익률·인건비율 추세 |
| `mart_voucher_monthly` | analytics.voucher_overview | 검진권 발급·사용·미사용·정산 |
| `mart_hr_turnover` | analytics.hr_turnover(by_dept·by_reason) | 진료과별 이직률·사유 |
> 각 마트 = refresh_* 함수 + 야간 잡 추가(기존 P0 패턴). PII 없음·집계만 유지.

## 1. 경영 BI (Grafana) — 10종 (대시보드·시계열·알림 중심)
| # | 항목 | 선행 | OSS |
|---|---|---|---|
| 1 | **자금·현금흐름 대시보드**(영업/투자/재무 추세 + 6개월 예측) | mart_cashflow·forecast | ✅ |
| 2 | **채권·채무 Aging 대시보드**(연령버킷 stacked bar + 회수율) | mart_ar_ap_aging | ✅ |
| 3 | **재고 운영 대시보드**(재고자산·회전율·안전재고미달·장기재고) | mart_inventory | ✅ |
| 4 | **재무비율 추세 대시보드**(유동/부채/의료이익률 + 임계 thresholds) | mart_ratios | ✅ |
| 5 | **검진권 운영 대시보드**(발급/사용/미사용잔액/정산콜백) | mart_voucher | ✅ |
| 6 | **HR 대시보드**(진료과별 이직률·입퇴사·사유 분포) | mart_hr_turnover | ✅ |
| 7 | **손익 구성 대시보드**(계정/기능별 수익→비용→순이익, YoY) | mart_income_lines | ✅ |
| 8 | **진료과 템플릿 변수**(dept 드롭다운으로 전 패널 동적 필터) | 현 dept 마트 | ✅ |
| 9 | **알림(Unified Alerting)**: 미검토 감사건·자금 최저잔액·부채비율 임계 → 이메일/웹훅 | 알림채널 설정 | ✅ |
| 10 | **연/월 대시보드 변수 + 전년비교**(time range·YoY 오버레이) | 현 exec 마트 | ✅ |
> ⚠ Grafana **스케줄 PDF 리포트는 Enterprise** — 정기 발송은 Metabase Subscriptions(아래 #6)로 대체.

## 2. 셀프서비스 BI (Metabase) — 10종 (모델·셀프탐색·구독 중심)
| # | 항목 | 선행 | OSS |
|---|---|---|---|
| 1 | **Models(모델)**: 각 마트를 모델로 정의(컬럼 한글명·타입·의미) → 비개발자 질문 용이 | 마트 | ✅ |
| 2 | **큐레이션 질문 세트**(수익추세·부서손익·재무비율·이직·재고·검진권·감사 10+) | 마트 | ✅ |
| 3 | **대시보드 + 파라미터 필터**(월·진료과 인터랙티브 필터) | 마트 | ✅ |
| 4 | **Metrics(공용 지표)**: 월수익·순이익률·이직률·회전율 재사용 정의 | 마트 | ✅ |
| 5 | **Segments(세그먼트)**: 보고부문 진료과·적자부서·미검토 감사 등 | 마트 | ✅ |
| 6 | **Subscriptions(구독)**: 경영진에 월간 대시보드 이메일 자동 발송 | SMTP 설정 | ✅ |
| 7 | **Alerts(질문 알림)**: 미검토 감사·적자부서 임계 시 알림 | SMTP 설정 | ✅ |
| 8 | **컬렉션 구조 + 권한**(경영/재무/감사 분류·역할별 접근) | — | ✅ |
| 9 | **Click behavior 드릴**(부서 클릭 → 부서 상세 질문 이동) | 마트 | ✅ |
| 10 | **한글 메타데이터·표시형식**(컬럼 한글명·원화/%·X-ray 자동탐색) | — | ✅ |
> ⚠ Subscriptions/Alerts는 **Metabase SMTP 설정** 선행(MB_EMAIL_* 또는 관리자 SMTP). per-user 식별은 공용계정 모델 한계(Enterprise SSO 시 해소).

## 3. 단계 권장
- **A. 마트 확장(P0-d)**: 위 §0 7개 마트 추가(기존 패턴 반복·저위험). → 두 도구 패널/질문 즉시 확장 가능.
- **B. Grafana**: 마트별 대시보드(1·2·3·5·6·7) + 템플릿 변수(8) + 알림(9). 프로비저닝 JSON으로 코드화.
- **C. Metabase**: Models(1)+질문세트(2)+대시보드필터(3) → Metrics/Segments(4·5) → SMTP 후 Subscriptions/Alerts(6·7).
- **D. 운영 선결**: Metabase SMTP·per-user 정책·외부노출 하드닝(공통).

## 결론
**경영 BI 10종·셀프 BI 10종 모두 구현 가능**. 대부분 **공통 마트 확장(§0)** 위에서 도구별 강점(Grafana=시계열·알림·변수 / Metabase=모델·셀프탐색·구독)으로 갈라진다. 신규 도메인 데이터 없이 **기존 집계 메서드 마트화 + 도구 설정**으로 달성. PII 없음 원칙 유지.
