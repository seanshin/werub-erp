# 검진권 딜러 거래·정산 계정 매핑표 (B-G1-B/C) — 제안·재무 확정 대기

> 대상: ERP 재무 + HIS 정산 payload 정합 · 발신: ERP(infra-core)
> 작성일: 2026-06-18 · 상태: **제안(DRAFT) — 회계 처리는 재무 사인오프 전 미구현**
> 원본: `his-voucher-dealer.response.md` §3~§4 · 계정 grounded: `finance.accounts`(마이그 0003~0032)
>
> ⚠️ **이 문서는 추측이 아닌 ERP 실 계정과목·전표 패턴에 근거한 초안이나, 검진권 회계의
> 수익인식 시점·수수료 이중계상 등은 재무 판단 사항이다. 확정 회신 전에는 B-G1-B/C를 구현하지
> 않는다(잘못된 전표 방지).** 확정되면 그대로 핸들러+신설계정 마이그로 반영.

---

## 0. 신설 필요 계정 (현재 ERP `finance.accounts`에 없음)

검진권 회계에 필요하나 부재 → 마이그(예: 0072)로 INSERT 필요. 제안 코드(기존 채번 관례 준수):

| 제안코드 | 명칭 | 종류 | 용도 |
|---------|------|------|------|
| **2140** | 선수금(검진권) | LIABILITY | 딜러 선결제 — 아직 미사용(미실현) 검진권 |
| **2150** | 예수금(원천징수) | LIABILITY | 국내 딜러 정산 시 원천징수액(납부 전) |
| **5520** | 지급수수료(검진권) | EXPENSE | 딜러 재판매 수수료 |
| **5660** | 외환차손 | EXPENSE | 해외 딜러 정산 환율 손실 |
| **4630** | 외환차익 | REVENUE | 해외 딜러 정산 환율 이익 |
| (선택) 4120 | 검진권수익 | REVENUE | 4110 외래수익과 분리 추적 시(미분리면 4110 사용) |

> 기존 사용 계정: **1110**현금 · **1130**미수금(카드) · **2110**미지급금 · **4110**외래수익.

---

## 1. B-G1-B 거래라인 (`voucher.line_recorded`) — 권장 매핑

전제(아래 §3 결정 필요): **수익은 redeem 시점 인식**(발생주의), 선결제는 선수금.

| lineType | 트리거 | 차변(Dr) | 대변(Cr) | 금액 | 비고 |
|----------|--------|----------|----------|------|------|
| **purchase** | 딜러 선결제 매입(병원→딜러) | 1110 현금 | **2140 선수금** | `totalAmount` | 선수금(부채) 인식. DB-3 |
| **resale** | 딜러→환자 재판매 | — | — | — | **병원 무전표**(딜러 장부) — §3-③ 확인 |
| **redeem** | 검진권 검진 사용 | **2140 선수금** | 4110(또는 4120) | `amountUsed` | 수익 실현(선수금 해소) |
| **topup** | 액면 초과 차액 | 1110/1130 | 4110 | `amount` | 환자 추가청구(일반 수익) |
| **refund** | 소비자 환급 | **2140 선수금** | 1110 현금 | `refundAmount` | 미사용분 환급(선수금 해소) |
| **commission** | 재판매 수수료 | **5520 지급수수료** | 2110 미지급금(딜러) | `commissionAmount` | clawback(음수)=역분개(Dr 2110 / Cr 5520) |

---

## 2. B-G1-C 정산 (`voucher.settled`) — AP 지급, 권장 매핑

전제: 수수료는 §1 commission 이벤트로 **미지급금 기적립** → 정산은 **지급 실행**(이중계상 회피, §3-① 결정).

**국내(DOMESTIC) — 원천징수·세금계산서**
```
Dr 2110 미지급금        netPayable(=기적립 수수료)
   Cr 2150 예수금(원천징수)   withholdingTax
   Cr 1110 현금             netPayableAfterTax
```
> `withholdingTax`/`netPayableAfterTax`는 HIS payload(B2). 원천징수율=설정값(§3-④). 세금계산서 발행은 별도(§3-④).

**해외(OVERSEAS) — 외화·환율**
```
Dr 2110 미지급금        netPayable(적립 원화 기준)
   Cr 1110 현금             netPayable × fxRate (송금 원화)
   Dr 5660/Cr 4630 외환차손익  적립환율 vs 송금환율 차이
```
> `fxRate`는 HIS payload 스냅샷 사용(ERP 환율 마스터 없음, §3-⑤). 원천징수 비대상(조세조약 가정).

> **대안 모델(§3-① 'settlement-only')**: commission 이벤트를 무전표로 두고 정산에서 일괄
> `Dr 5520 지급수수료 / Cr 2150 예수금 + 1110 현금`. 이 경우 §1 commission 행은 정보성.

---

## 3. 재무 확정 필요 결정 (구현 전 회신 요망)

| # | 결정 | ERP 권장(가정) | 영향 |
|---|------|----------------|------|
| ① | **수수료 인식 시점** — 거래별(commission 이벤트) vs 정산시(settlement) | 거래별 적립→정산 지급 | **이중계상 위험**: 둘 다 전표화 금지. 택1. settlement.netPayable이 적립 미지급과 대사돼야 |
| ② | **수익 인식 시점** | **redeem**(발생주의·서비스 제공시) | resale/purchase 인식이면 선수금 구조 전면 변경 |
| ③ | **resale 전표 여부** | 병원 무전표(딜러 장부) | 매출 인식이면 이중계상 |
| ④ | **원천징수**(DB-1) | 율=설정값(잠정 3.3% 사업소득?)·세금계산서 주체 | 국내 정산 전표 |
| ⑤ | **환율**(DB-2) | HIS payload `fxRate` 스냅샷 사용 | 해외 정산·외환차손익 |
| ⑥ | **선결제 계정**(DB-3) | 2140 선수금(부채) | vs 매입(자산) |
| ⑦ | **검진권 수익 계정** | 4110 외래수익 통합 | vs 4120 분리(BI·세무 분리 필요시) |

---

## 4. 구현 메모(확정 후)

- 신설계정 마이그(0072): `finance.accounts` INSERT(위 §0). 기존 0003 패턴(code/name/kind).
- 핸들러: `_voucher_line`(lineType 분기→`VoucherService.create` Dr/Cr DTO), `_voucher_settled`
  (→`SettlementService.record_payable`/`pay` 확장 또는 직접 전표). 멱등=전표 `(source_module,
  source_ref)` UNIQUE(`voucher:<lineType>:<refId>`).
- `finance.Payable` 확장: vendor_id FK(scm.suppliers)·withholding_tax·fx_rate(현재 generic).
- 정산 지급 워크플로(DB-4): AP 승인 후 지급→§6 HIS 회신.
- prod 인박스 DEAD 4건(Blocker1 전 검진권 거래)은 B/C 개통 후 HIS 재발행으로 처리.
