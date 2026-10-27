# 계약 — ERP → HIS 정산 지급 회신 (settlement.paid 콜백) [DB-4]

> 발신: ERP(infra-core) · 대상: HIS(HospitalRUN) B3 인바운드 EP 구현용
> 작성 2026-06-18 · 상태: **계약 초안 전문(ERP 제시)** — HIS가 이 스펙대로 수신 EP 구현
> 방향: **ERP → HIS**(기존 HIS→ERP 웹훅의 역방향). ERP가 딜러 정산 지급을 실행한 뒤 HIS에 회신.

---

## 1. 엔드포인트 (HIS가 구현)

```
POST {HIS_BASE}/api/v1/voucher/settlements/erp-callback
Content-Type: application/json
X-Integration-Key: <상호 합의 키>      # ERP→HIS 방향 인증(대칭키 권장)
```

- 인증: ERP는 서버 `.env`의 송신키(예 `HIS_INTEGRATION_KEY`)로 헤더 부착. HIS는 상수시간 비교.
  (HIS→ERP 웹훅의 `X-Integration-Key`와 동일 패턴, 방향만 반대.)
- 전송 보장: ERP outbox 지수백오프 재시도(전송 실패 시 재시도). **HIS는 멱등 수신 필수**.

## 2. Body 스키마

```json
{
  "event_id": "erp:settle-paid:<settlementId>",   // 멱등키(재전송 동일값) — HIS UNIQUE
  "settlementId": "<HIS 정산 id>",                 // 우선 식별자
  "distributorId": "<딜러 id>",                    // settlementId 없을 시 대체키
  "period": "2026-06",                             // distributorId+period 조합 식별 보조
  "status": "PAID",                                // PAID | PARTIAL
  "paidAt": "2026-06-18T09:00:00+09:00",           // ISO8601 지급 시각
  "erpVoucherRef": "ap-pay-<payableId>-1",         // ERP 지급 전표 source_ref(추적)
  "amountPaidKrw": 4380200,                        // 실 지급 원화
  "withholdingKrw": 19800,                         // 원천징수액(DB-1 확정 후·없으면 0/생략)
  "fxRateApplied": 1350.5                           // 외화 송금 시 적용 환율(없으면 생략)
}
```

| 필드 | 필수 | 비고 |
|------|------|------|
| event_id | ✅ | `erp:settle-paid:<settlementId>` — HIS 멱등 UNIQUE |
| settlementId | ✅* | settlementId 또는 (distributorId+period) 중 하나 필수 |
| status | ✅ | PAID(전액) / PARTIAL(부분) |
| paidAt | ✅ | 지급 시각 |
| erpVoucherRef | ✅ | ERP 전표 추적키 |
| amountPaidKrw | ✅ | 실 지급 원화 |
| withholdingKrw | ⬜ | DB-1 확정 후 |
| fxRateApplied | ⬜ | 외화 송금 시 |

## 3. HIS 처리 기대

- **멱등**: `event_id` UNIQUE — 동일 재전송 무시(중복 PAID 반영 금지).
- **상태가드**: `VoucherSettlement.status` DRAFT/CONFIRMED → PAID(또는 PARTIAL)만 전이.
  이미 PAID면 무시(멱등). 역전이 금지.
- 응답: `200 {received:true}`(수신·반영). 검증 실패 `4xx`(ERP가 재시도 안 함 — 영구실패 가시화).
  일시 오류 `5xx`(ERP outbox 재시도).

## 4. ERP 측 트리거 (참고)

ERP는 `voucher.settled` 수신 → `finance.Payable`(AP) 생성 → **ERP 관리자 승인 후 지급**
(SettlementService.pay, 직무분리) → 지급 전표 확정 → **본 콜백 1회 송신**. 부분지급 시 status=PARTIAL,
잔액 완납 시 PAID 재송신(다른 erpVoucherRef·동일 event_id 규칙은 `:seq` 부가로 구분).

> ⚠️ 미해소 선행: **DB-1 원천징수율·세금계산서 주체**(→ withholdingKrw 채움), **외화 딜러 정산
> 회계 모델**(거래별 적립이 외화는 보류라 settlement시 미지급 잔액 부재 — 외화는 정산시 비용인식
> 여부 재무 확정 필요). 위 둘 확정 시 ERP가 `_voucher_settled` 핸들러 + 본 콜백 송신 구현.
