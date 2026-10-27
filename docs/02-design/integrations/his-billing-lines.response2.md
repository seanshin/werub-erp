# [ERP→HIS] 진료라인 회신서에 대한 확정 답변 — D-6 (2차)

> 발신: ERP · 수신: HIS 팀 · 작성일: 2026-06-11
> 대상: HIS 회신서 `his-billing-lines.response.md` §5 확인 4건
> 결론: **4건 전부 확정 — HIS는 §4 개발(요청1·3)만 진행하면 됨. ERP 측 보정은 구현·배포 완료.**

## §5 확인 4건 답변

| # | 질문 | ERP 확정 답변 |
|---|------|--------------|
| 1 | coverage enum에 MEDICAID 포함? | **✅ 포함** — ERP enum은 `MEDICAL_AID`. **매핑은 ERP 책임으로 구현 완료**: HIS는 원enum(NHI·MEDICAID·SELF_PAY·AUTO·INDUSTRIAL) 그대로 송신하면 ERP가 변환(NHI→NHIS, MEDICAID→MEDICAL_AID, SELF_PAY→NONCOVERED). `insuranceCategory=NON_COVERED`는 라인 우선으로 비급여 처리(§2.2 그대로) |
| 2 | code 키 체계 | ERP fee_master 키 = **심평원 EDI 코드 문자열**(예: AA157) — **ERP가 HIS로 push한 procedure-codes 161,735건과 동일 체계**. HIS `ProcedureCode.code`가 그 push분이면 **무매핑**. 검증 안전망: 미등재 코드는 ERP가 invoice `warnings` + 사전심사 UNKNOWN_CODE 룰로 즉시 가시화 — 송신 시작 후 첫 주 unknown 리포트로 상호 확인하면 충분 |
| 3 | 백필 방식 | **API pull 채택** — ERP가 `GET /api/v1/integration/billing/lines?from=&to=` 호출(X-Integration-Key 공유키). ERP 측 puller 구현 완료(`POST /admin/integrations/billing/backfill`, 건별 격리·멱등 치환) — HIS가 EP만 제공하면 즉시 백필 가능 |
| 4 | payment.recorded 우선순위 | **P2(후순위)** — 당장은 기존 수납 미러(billing.settled·his_payment 동기화) 차감으로 근사. 중간 진료비 운영 후 기수납 오차가 유의하면 그때 도입 협의 |

## ERP 측 구현 현황 (운영 배포 완료)

- `billing.line_recorded` consumer: lineId 멱등 치환·cancelled·**HIS coverage enum 매핑 내장**
- 진료비 책정: 시점 단가 × 본인부담 분리(입원20/외래40/식대50/특례5/비급여/자보·산재·의료급여) — ③ 회신대로 **산정특례·의료급여 종별은 ERP 룰 판정**(현재 단순화, 정밀화는 원무 협의 파라미터). 참고: HIS `copayRate` 동봉 환영 — ERP 계산과 대사(검증)용으로 사용 예정
- 수납 창구 API: `GET {ERP}/api/v1/integration/billing/invoice?chartNo=&encounterId=&interim=true` — **스펙 확정, 운영 라이브**(§3 B안). 응답: `total·patient_share·insurer_share·non_covered·prepaid·balance_due·pre_review.blocked`
- 백필 puller: 위 3번

## 다음 단계

1. HIS: 요청1(`billing.line_recorded` 발행) + 요청3(백필 EP) 개발 → 송신 시작 통보
2. 스테이징 E2E: 라인 송신 → ERP 케이스/중간계산 → 수납 창구 invoice 조회 라운드트립
3. 첫 주 unknown code 리포트 상호 확인 (② 검증)
