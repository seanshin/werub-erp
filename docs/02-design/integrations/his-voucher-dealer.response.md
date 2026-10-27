# ERP → HIS 회신 — 검진권 딜러·거래 연동 (B-G1)

> 대상: HIS(HospitalRUN) 개발팀 · 발신: ERP(infra-core)
> 작성일: 2026-06-18 · 버전: v1.0 · 상태: 회신(일부 비즈니스 결정 대기)
> 원본 핸드오프: `Hospital RUN/docs/05-reference/his-to-erp-voucher-dealer-spec.md` v1.0
> 코드 grounded: `services/core/src/erp/integrations/{his_webhook_router,inbound_consumer,models}.py`,
> `finance/{models,settlement_service}.py`, `scm/models.py`, `config.py` (2026-06-18 main)

---

## 0. 한 줄 결론

**수신 인프라(웹훅 EP·인증·멱등 인박스·재시도/DLQ·운영 재처리 API)는 이미 가동 중이라 재사용**한다.
신규로 만들 것은 **(a) 딜러 거래처 upsert 핸들러, (b) 검진권 거래라인 핸들러(lineType별 회계),
(c) 정산 AP 핸들러**의 3개 도메인 핸들러 + 약간의 모델 확장뿐.

**단, 가장 먼저 합의해야 할 블로커가 하나 있다 → §1 (event_type 충돌).**

---

## 1. 🔴 블로커 — `billing.line_recorded` event_type 충돌 (HIS 조치 요청)

핸드오프 §3은 검진권 거래를 `event_type: "billing.line_recorded"`로 발행한다고 했는데,
**ERP는 이미 이 event_type을 임상 진료비 라인(`patient.BillingLine`) 수신에 사용 중**이다.

- 디스패치 키는 `(domain, event_type)` 튜플이다(`inbound_consumer.py:435` `HANDLERS`).
  `("finance", "billing.line_recorded") → _billing_line` 이 이미 등록돼 있다.
- `_billing_line`은 `lineId`·`chartNo`·`code`(EDI 문자열 ≤20자)를 **필수**로 요구하고
  없으면 `ValueError("lineId/chartNo/code 필수")`를 던진다(`inbound_consumer.py:141-145`).
- 검진권 payload(`serial`/`commissionAmount`/`distributorId`, chartNo·code 없음)를 같은
  타입으로 보내면 → **매 건 ValueError → FAILED → (재시도 5회 후) DEAD**. 회계 효력 0, 인박스 오염.

### ✅ 요청: 검진권 이벤트는 **전용 event_type**으로 발행해 달라

`(domain, event_type)` 라우팅이라 **도메인은 `finance` 유지**해도 되고, event_type만 분리하면
기존 임상 경로를 전혀 건드리지 않고 신규 핸들러로 깔끔히 라우팅된다. ERP 제안:

| HIS 발행(핸드오프) | → ERP 수신 event_type(제안) | 핸들러(신규) |
|--------------------|------------------------------|--------------|
| `vendor.synced` / `vendor.changed` | 그대로 사용 OK | `_voucher_vendor` |
| `billing.line_recorded` (source=voucher) | **`voucher.line_recorded`** | `_voucher_line` |
| settlement(lineType) | **`voucher.settled`** (별도 타입 권장) | `_voucher_settled` |

> `vendor.synced`/`vendor.changed`는 ERP에 동명 핸들러가 없어 충돌 없음 → 그대로 수용.
> 거래라인·정산만 `voucher.*`로 분리하면 됨. (대안: HIS가 타입 분리를 못 하면 ERP가
> `_billing_line` 진입부에서 `payload.source=="voucher"` 분기 가능하나, **타입 분리가 정석**이고
> 인박스 가시성·운영 분류상 권장.)

---

## 2. §7 확정 요청 항목 — ERP 회신

| ID | 항목 | ERP 회신 |
|----|------|----------|
| **B-G1** | 수신 핸들러 구현 여부·일정 | **구현 가능. 인프라 재사용으로 핸들러 3종만 신규.** 단계 계획 §3. event_type 분리(§1)·비즈니스 결정(DB-1/2/3) 확정 시 착수. |
| **인증** | 웹훅 인증 방식 | **이미 확정·가동 중.** ERP는 헤더 **`X-Integration-Key`** 를 요구하고 서버 `.env`의 `HIS_WEBHOOK_KEY`와 **상수시간 비교**(`hmac.compare_digest`)한다(`his_webhook_router.py:88-91`). HIS는 이 헤더만 부착하면 됨(키는 이미 공유된 `his_webhook_key`). 평문 POST → 인증 부착 필요. |
| **멱등** | inbox UNIQUE 키 수용 | **수용·가동 중.** `his_inbound_events.source_ref` UNIQUE(`models.py:20`). `event_id`(=source_ref)로 중복 무시. ⚠️ **단, 갱신성 주의**: ERP는 `_UPDATABLE_EVENTS`에 한해 동일 ref 재수신 시 payload 갱신+재처리한다. 검진권은 **상태변경/정산 재발행이 동일 ref면** 그 목록에 추가 필요(§3-D). |
| **DB-1** | 국내 원천징수율·세금계산서 주체 | **비즈니스 결정 필요(ERP 단독 불가).** ERP 기본 입장: 원천징수율은 **DB 상수가 아니라 설정값**으로 둠(법무 확정 후 변경 가능). 세금계산서 발행 주체·역발행 여부 회신 요망. |
| **DB-2** | 외화 환율 소스·시점 | **비즈니스 결정 필요.** ERP는 환율 마스터가 없음 → HIS가 **정산 payload에 `fxRate` 스냅샷 동봉**하는 방식 선호(ERP는 수신값 저장·표시, 자체 환율 조회 안 함). BOK 기준환율 제안 수용 가능하나 **조회·확정 책임은 HIS**. |
| **DB-3** | 선결제 매입 계정(선수금 vs 매입) | **비즈니스 결정 필요.** 회계상 **선급금(자산)** 후 redeem 시 비용/수익 실현 매칭이 정합적이라 보나, 확정은 재무 결정. 신규 계정코드 필요 시 마이그로 추가. |
| **DB-4** | 정산 지급 워크플로우 + §6 회신 | **ERP AP 승인 워크플로 보유.** 정산→`finance.Payable`(AP) 생성 후 ERP 내부 승인·지급. 지급 완료 시 §6 회신 가능(아웃바운드). HIS가 `POST /voucher/settlements/erp-callback` 열어주면 ERP가 호출. |

---

## 3. 단계 구현 계획 (event_type 분리·결정 확정 후 착수)

전제: §1 event_type 분리, DB-1/2/3 회신. 각 단계는 독립 PR.

- **B-G1-A 딜러 거래처 upsert** (`_voucher_vendor`): `vendor.synced/changed` → `scm.Supplier` upsert
  (`external_vendor_code`=dealerId 매칭). **모델 확장**: `dealer_type`(DOMESTIC/OVERSEAS·세무구분),
  `commission_rate`, `country`/`currency`, `status`(TERMINATED→비활성). `business_no`는 DOMESTIC만
  필수(현재 NOT NULL UNIQUE → **OVERSEAS 위해 nullable 완화 필요**, 마이그).
- **B-G1-B 거래라인 회계** (`_voucher_line`): `voucher.line_recorded` lineType 분기 —
  resale(매출·수취), commission(±, clawback 음수), redeem(검진수익 인식), topup(추가청구),
  refund(환급 지급). 각 lineType→전표 자동생성. **계정 매핑표는 재무 확정 필요**(별첨 요청).
  멱등=`voucher:<lineType>:<refId>`.
- **B-G1-C 정산 AP** (`_voucher_settled`): netPayable → `finance.Payable` 생성
  (`source_module="his.voucher"`, `source_ref`=settlementId). DOMESTIC=원천징수 차감·세금계산서,
  OVERSEAS=외화(fxRate 적용). 기존 `SettlementService.record_payable`/`pay` 재사용.
  **모델 확장**: Payable에 `vendor_id` FK·세무구분·`withholding_tax`/`fx_rate` 필드(현재 generic).
- **B-G1-D 멱등·갱신 정합**: 정산/상태변경 중 동일 ref 재발행되는 이벤트를 `_UPDATABLE_EVENTS`에
  반영. 선결제(§5 purchase)는 HIS 발행 개시 시 `_voucher_purchase` 추가(DB-3 결정 후).
- **B-5 지급 회신**(선택): ERP 지급 완료 → HIS `erp-callback` 호출(아웃바운드 키 공유 후).

---

## 4. 재사용되는 기존 인프라 (신규 구현 불요)

| 구성 | 위치 | 비고 |
|------|------|------|
| 웹훅 수신 EP | `his_webhook_router.py:82` `POST /integrations/his/{domain}/webhook` | `finance` 도메인 허용됨 |
| 인증 | 동 `:88-91` `X-Integration-Key` HMAC 상수시간 | `HIS_WEBHOOK_KEY` |
| 멱등 인박스 | `models.py:16` `HisInboundEvent`(source_ref UNIQUE) | NEW/APPLIED/FAILED/DEAD |
| 소비 파이프라인 | `inbound_consumer.py` 실시간(Redis pub) + 5분 폴링 안전망 | `(domain,event_type)→handler` |
| 재시도/DLQ | 동 `:494` 선형백오프 5회→DEAD | |
| 운영 재처리 API | `his_webhook_router.py:/inbound`·`/inbound/{id}/requeue`, `admin_router` `/admin/integrations/inbound/consume` | ADMIN |
| AP 원장 | `finance.Payable` + `SettlementService` | generic→확장 |

---

## 5. HIS에 회신 부탁(ERP 착수 전 필요)

1. **(블로커) event_type 분리 수용 여부** — §1 표대로 `voucher.line_recorded`/`voucher.settled` 발행 가능?
2. **DB-1** 원천징수율(잠정 3.3%?)·세금계산서 발행 주체/역발행.
3. **DB-2** 환율: 정산 payload에 `fxRate` 스냅샷 동봉 방식 수용?
4. **DB-3** 선결제 매입 계정 처리(선급금 가정 수용?).
5. **lineType별 계정 매핑표** — resale/commission/redeem/topup/refund 각각의 차·대변 계정(재무 확정).
6. **§6 회신 채널** 인증키·바디 스키마 최종.

> ERP는 1·3은 즉시 수용 가능(코드 정합), 2·4·5는 ERP 재무/사용자 결정 후 확정. 위 회신 도착 시
> §3 단계 PR 착수.
