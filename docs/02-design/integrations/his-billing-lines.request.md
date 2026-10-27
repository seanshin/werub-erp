# [ERP→HIS] 진료 라인 인터페이스 요청서 — 진료비 책정·사전심사 (D-6)

> 발신: ERP · 수신: HIS 팀 · 작성일: 2026-06-11
> 근거 결정: **D-6 진료비 SoT** — HIS 원무=접수·수납, **ERP=진료비 책정·사전 검토·계산**
> (`docs/02-design/integrations/billing-sot-decision.md`)
> 공통: W3 봉투 `{event, source, ref, occurredAt, payload}` · `X-Integration-Key` · ref 멱등 (기존 동일)

## 0. 목적 — 라인 피드 하나로 세 업무

1. **사전심사 자동 적재**: 워크리스트(기구현)가 심사팀 붙여넣기 없이 자동 채워짐
2. **중간 진료비 책정**: 재원 환자 누적 라인 × 시점 단가 × 본인부담 분리
3. **퇴원/외래 정산·청구액 산출**: ERP 계산 결과를 HIS 수납 창구가 표시·수납

## 1. [요청 1·P0] 진료행위 라인 이벤트 — `billing.line_recorded` (신규)

**수신 EP(기존 게이트웨이)**: `POST {ERP_BASE}/api/v1/integrations/his/finance/webhook`

```jsonc
{
  "event": "billing.line_recorded",
  "source": "his",
  "ref": "bline:{encounterId}:{lineId}",      // 멱등 — 정정 시 동일 lineId 재송신(덮어쓰기)
  "occurredAt": "2026-06-15T10:30:00+09:00",
  "payload": {
    "chartNo": "C-2026-0001",
    "encounterId": "enc-uuid",                 // 내원/입원 에피소드
    "encounterType": "OUTPATIENT|INPATIENT",
    "admittedOn": "2026-06-10",                // 입원 시 (중간계산 기간 기산)
    "deptCode": "IM",
    "doctorStaffNo": "2026-0001",
    "lineId": "line-uuid",
    "code": "AA157",                           // 수가/약가/재료 코드 (ERP fee_master 키)
    "codeType": "TARIFF|DRUG|MATERIAL",
    "count": 1,
    "treatedOn": "2026-06-15",
    "treatedAt": "2026-06-15T10:30:00+09:00",  // 야간·공휴 가산 판정
    "patientAge": 67, "patientSex": "M",       // 연령·성별 제한 룰
    "coverage": "NHIS|NONCOVERED|AUTO|INDUSTRIAL",  // 보험 구분(알면)
    "cancelled": false                          // true = 라인 취소(행위 취소·처방 변경)
  }
}
```

- 송신 시점: 처방/행위 **확정 시 즉시**(권장) 또는 일 1회 배치 — 즉시면 중간계산이 항상 최신
- 정정/취소: 동일 `lineId` 재송신(`cancelled:true` 포함) — ERP가 치환

## 2. [요청 2·P0] 계산 결과 회신 — ERP가 제공 (HIS 수납 창구용)

ERP가 신설 예정(라인 스키마 합의 후 구현):

- `GET {ERP_BASE}/api/v1/integration/billing/invoice?chartNo=&encounterId=&interim=true`
  (Header `X-Integration-Key` — 기존 공유 키)
  → `{ chartNo, encounterId, asOf, lines[], total, insurerShare, patientShare,
       nonCovered, prepaid, balanceDue, preReview: {blocked, findings} }`
- **interim=true**: 재원 중 누적분 = **중간 진료비**. 미수납분(balanceDue)은 기수납(HIS 수납 동기화분) 차감 후
- `preReview.blocked=true`면 수납 전 심사팀 확인 권장(사전심사 BLOCK 룰 — 창구 표시 여부는 HIS UI 재량

**HIS 확인 요청**: 수납 화면이 이 API를 조회하는 방식(B안 pull) vs ERP가 계산 시마다 push(A안) — **B안 권장**(수납 시점 최신 보장)

## 3. [요청 3·P1] 과거분 백필

운영 전환 시 재원 중 환자의 기존 발생분 일괄 적재:
`GET {HIS}/api/v1/integration/billing/lines?from=&to=` (HIS 제공) 또는 1회성 export 파일 — 방식 회신 요청

## 4. 미결 질문 (HIS 회신)

- [ ] 라인 송신 시점: 즉시 이벤트 vs 일배치 (ERP는 양쪽 다 수용 — 즉시 권장)
- [ ] `coverage`(보험 구분)를 HIS가 알고 있는지 — 모르면 ERP가 fee_master·비급여 목록으로 판정
- [ ] 본인부담률 산정에 필요한 환자 속성(산정특례 코드·의료급여 종별) 보유·전달 가능 여부
- [ ] 백필 방식(§3)
- [ ] 수납 완료는 기존 `billing.settled`(일마감 합계) 유지 — **건별 수납 이벤트**(`payment.recorded`, chartNo·금액) 추가 가능한지 (중간계산의 기수납 차감 정밀화)

## 5. ERP 측 후속 구현 (스키마 합의 후, 마이그 1건+모듈 1개)

라인 스테이징·치환 consumer → 책정 엔진(시점 단가 ✅기구현 · 본인부담 분리 룰 신규 · 비급여는 NonCoveredItem 16건 공유분 합류) → 중간계산서 발행(PDF/xlsx ✅패턴 보유) → 사전심사 자동 적재(✅기구현 save_run 재사용)
