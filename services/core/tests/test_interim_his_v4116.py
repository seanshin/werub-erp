"""HIS v4.116.0 입원 중간정산 수납 정비 수신(요청서 2026-10-22·0266) — R1 수단별 계정·R3 무효·R4 일마감 참고 칸·
R5 환불·R6 창구 기수납·상태 역행 방지."""
from datetime import date

from sqlalchemy import func, select

from erp.finance.models import Voucher, VoucherLine
from erp.integrations.inbound_consumer import InboundConsumer, _billing_line
from erp.integrations.models import HisInboundEvent
from erp.patient.billing_service import BillingService
from erp.patient.models import InterimBilling, InterimBillingRefund

consumer = InboundConsumer()

_BASE = {
    "interimBillingId": "IB-9", "admissionId": "ADM-9", "chartNo": "HR-0009",
    "sequenceNo": 1, "periodStart": "2026-10-19T00:00:00.000Z", "periodEnd": "2026-10-22T00:00:00.000Z",
    "totalAmount": 150000, "insuranceAmount": 120000, "copayAmount": 30000,
    "paidAmount": 0, "paidAt": None, "status": "GENERATED",
    "days": 3, "dailyRate": 50000, "stayCode": "AB1A0100", "priceBasis": "DEFAULT_CONSTANT", "copayRate": 0.2,
}
_PAID = dict(_BASE, status="PAID", paidAmount=30000, paidAt="2026-10-22T10:00:00+09:00", paidBy="u-1")


def _ev(event_type, ref, payload):
    return HisInboundEvent(domain="finance", event_type=event_type, source_ref=ref, payload=payload, status="NEW")


async def _lines(session, ref):
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ref))
    if v is None:
        return None
    return {(ln.account_code, ln.side): float(ln.amount) for ln in (await session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all()}


async def _status(session, ref):
    return (await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == ref))).status


def _gate(monkeypatch, on=True):
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "his_interim_gl_enabled", on, raising=False)


async def test_paid_card_posts_card_receivable_and_keeps_payments(session, monkeypatch):
    """R1 — CARD는 DR 1132 카드미수금, 원장 줄·처리자·산정 근거 보존."""
    _gate(monkeypatch)
    pays = [{"method": "CARD", "amount": 30000, "cardApprovalNo": "77001234", "cardCompany": "신한",
             "installment": 0, "cashReceiptNo": None, "processedBy": "u-1", "recordedAt": "2026-10-22T01:00:00Z"}]
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", dict(_PAID, payments=pays)))
    await session.flush()
    assert (await consumer.consume(session))["applied"] == 1
    assert await _lines(session, "interim:paid:IB-9") == {("1132", "DEBIT"): 30000, ("1131", "CREDIT"): 30000}
    row = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == "IB-9"))
    assert row.payments[0]["cardApprovalNo"] == "77001234" and row.paid_by == "u-1"
    assert row.price_basis == "DEFAULT_CONSTANT" and row.days == 3 and float(row.daily_rate) == 50000


async def test_paid_mixed_methods_split_and_transfer_to_cash(session, monkeypatch):
    """R1 — 배열(복합 결제 대비): CASH·TRANSFER는 1110 합산, CARD는 1132."""
    _gate(monkeypatch)
    pays = [{"method": "CASH", "amount": 10000}, {"method": "TRANSFER", "amount": 5000},
            {"method": "card", "amount": 15000, "cardApprovalNo": "1"}]
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", dict(_PAID, payments=pays)))
    await session.flush()
    await consumer.consume(session)
    assert await _lines(session, "interim:paid:IB-9") == {
        ("1110", "DEBIT"): 15000, ("1132", "DEBIT"): 15000, ("1131", "CREDIT"): 30000}


async def test_paid_sum_mismatch_or_unknown_method_goes_dead(session, monkeypatch):
    """R1 — 배열 합계 ≠ paidAmount, 미지원 수단(선수금·검진권)은 APPLIED로 삼키지 않는다."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_paid", "R-MIS", dict(_PAID, payments=[{"method": "CASH", "amount": 20000}])))
    session.add(_ev("billing.interim_paid", "R-UNK", dict(_PAID, interimBillingId="IB-8",
                                                           payments=[{"method": "VOUCHER", "amount": 30000}])))
    await session.flush()
    for _ in range(6):
        await consumer.consume(session)
    assert await _status(session, "R-MIS") in ("FAILED", "DEAD")
    assert await _status(session, "R-UNK") in ("FAILED", "DEAD")
    assert await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref.like("interim:paid:%"))) == 0


async def test_paid_without_payments_legacy_goes_cash(session, monkeypatch):
    """R1 — payments 없는 과거·재전송분은 1110(현행) + 적요 '수단 미기록'."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    await session.flush()
    await consumer.consume(session)
    assert await _lines(session, "interim:paid:IB-9") == {("1110", "DEBIT"): 30000, ("1131", "CREDIT"): 30000}
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "interim:paid:IB-9"))
    assert "수단 미기록" in v.memo


async def test_late_recorded_does_not_regress_paid(session):
    """재전송된 interim_recorded(GENERATED)가 PAID 회차의 수납액·상태를 덮지 않는다."""
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("billing.interim_recorded", "interim:rec:IB-9", _BASE))
    await session.flush()
    await consumer.consume(session)
    row = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == "IB-9"))
    assert row.status == "PAID" and float(row.paid_amount) == 30000


async def test_voided_marks_void_and_rejects_paid(session):
    """R3 — 무효는 상태 표시만(GL 없음). 수납된 회차의 무효·무효 회차의 수납은 DEAD."""
    session.add(_ev("billing.interim_recorded", "interim:rec:IB-9", _BASE))
    session.add(_ev("billing.interim_voided", "interim:void:IB-9", dict(
        _BASE, status="VOID", voidedAt="2026-10-22T02:00:00Z", voidedBy="u-2", voidReason="기간 오입력")))
    await session.flush()
    await consumer.consume(session)
    row = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == "IB-9"))
    assert row.status == "VOID" and row.void_reason == "기간 오입력" and row.voided_at is not None
    session.add(_ev("billing.interim_paid", "R-PAID-AFTER-VOID", _PAID))
    session.add(_ev("billing.interim_paid", "interim:paid:IB-7", dict(_PAID, interimBillingId="IB-7")))
    session.add(_ev("billing.interim_voided", "R-VOID-PAID", dict(_BASE, interimBillingId="IB-7", status="VOID")))
    await session.flush()
    for _ in range(6):
        await consumer.consume(session)
    assert await _status(session, "R-PAID-AFTER-VOID") in ("FAILED", "DEAD")
    assert await _status(session, "R-VOID-PAID") in ("FAILED", "DEAD")
    ib7 = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == "IB-7"))
    assert ib7.status == "PAID"


async def test_settled_ignores_interim_payments_field(session):
    """R4 — 일마감 summary.data.interimPayments(참고 칸)는 전기하지 않는다(byPaymentMethod만)."""
    session.add(_ev("billing.settled", "EVT-R4", {"date": "2026-10-23", "summary": {"data": {
        "totalRevenue": 100000, "byPaymentMethod": {"cash": 100000},
        "interimPayments": {"settledExcluded": True, "count": 2, "amount": 60000,
                            "byMethod": {"CARD": 30000, "CASH": 30000}, "unledgeredCount": 0, "unledgeredAmount": 0}}}}))
    await session.flush()
    await consumer.consume(session)
    assert await _lines(session, "settle:2026-10-23") == {("1110", "DEBIT"): 100000, ("4110", "CREDIT"): 100000}


async def test_refund_partial_reverses_and_caps(session, monkeypatch):
    """R5 — 부분 환불 DR 1131 / CR 수단별 · refundId 멱등 · 누적 > 수납 DEAD · 수납 전 환불 DEAD."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", dict(_PAID, payments=[{"method": "CARD", "amount": 30000}])))
    await session.flush()
    await consumer.consume(session)

    def rf(rid, amt, method="CARD"):
        return dict(_PAID, refund={"refundId": rid, "amount": amt, "method": method, "reason": "과납",
                                   "refundedBy": "u-3", "refundedAt": "2026-10-25T09:00:00+09:00"})
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-1", rf("RF-1", 10000)))
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-1:dup", rf("RF-1", 10000)))
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-2", rf("RF-2", 5000, "CASH")))
    await session.flush()
    await consumer.consume(session)
    assert await _lines(session, "interim:refund:RF-1") == {("1131", "DEBIT"): 10000, ("1132", "CREDIT"): 10000}
    assert await _lines(session, "interim:refund:RF-2") == {("1131", "DEBIT"): 5000, ("1110", "CREDIT"): 5000}
    assert await session.scalar(select(func.count()).select_from(InterimBillingRefund)) == 2
    session.add(_ev("billing.interim_refunded", "R-OVER", rf("RF-3", 20000)))
    session.add(_ev("billing.interim_refunded", "R-NOPAY", dict(rf("RF-4", 1000), interimBillingId="IB-NONE")))
    await session.flush()
    for _ in range(6):
        await consumer.consume(session)
    assert await _status(session, "R-OVER") in ("FAILED", "DEAD")
    assert await _status(session, "R-NOPAY") == "FAILED"            # 순서 뒤바뀜 = 일시 실패(재시도)


async def test_invoice_prepaid_includes_interim_paid_minus_refund(session):
    """R6 — 창구 중간 계산(interim=true) 기수납에 중간정산 수납이 들어가고 환불은 빠진다. 앞선 입원 회차는 제외."""
    from erp.regulatory.models import FeeMaster
    session.add(FeeMaster(code="AA100", code_type="TARIFF", name="처치료", unit_price=50_000,
                          valid_from=date(2026, 1, 1)))
    for i, d in enumerate((19, 20)):
        await _billing_line(session, {"lineId": f"L-IB-{i}", "chartNo": "HR-0009", "code": "AA100",
                                      "encounterType": "INPATIENT", "admittedOn": "2026-10-19",
                                      "treatedOn": f"2026-10-{d}"}, "billing.line_recorded", {})
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    session.add(_ev("billing.interim_paid", "interim:paid:IB-OLD", dict(
        _PAID, interimBillingId="IB-OLD", periodStart="2026-06-01", periodEnd="2026-06-05", paidAmount=99000)))
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-9", dict(
        _PAID, refund={"refundId": "RF-9", "amount": 5000, "method": "CASH"})))
    await session.flush()
    await consumer.consume(session)
    inv = await BillingService().invoice(session, chart_no="HR-0009")
    assert inv["prepaid"] == 25_000                       # 30,000 − 5,000 (6월 앞선 입원 99,000 제외)
    assert inv["balance_due"] == inv["patient_share"] - 25_000


# ── 독립 검토 보완(MED 4·LOW 4) ──────────────────────────────────────────────

async def test_refund_before_paid_retries_then_applies(session, monkeypatch):
    """MED1 — 환불이 수납보다 먼저 와도 DEAD가 아니라 일시 실패 → 수납 수신 뒤 재시도로 반영."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-E", dict(
        _PAID, refund={"refundId": "RF-E", "amount": 5000, "method": "CASH"})))
    await session.flush()
    await consumer.consume(session)
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "interim:refund:RF-E"))
    assert ev.status == "FAILED"
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", dict(_PAID, payments=[{"method": "CASH", "amount": 30000}])))
    ev.next_retry_at = None
    await session.flush()
    await consumer.consume(session)                    # 같은 묶음에서 환불이 먼저 돌면 다시 FAILED — 수납은 반영
    ev.next_retry_at = None
    await session.flush()
    await consumer.consume(session)
    assert await _status(session, "interim:refund:RF-E") == "APPLIED"
    assert await _lines(session, "interim:refund:RF-E") == {("1131", "DEBIT"): 5000, ("1110", "CREDIT"): 5000}


async def test_paid_replay_with_different_method_goes_dead(session, monkeypatch):
    """MED2 — 수단 없이 전기된 수납이 CARD로 재전송되면 전표 멱등에 묻히지 않고 DEAD."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("billing.interim_paid", "R-REPLAY", dict(_PAID, payments=[{"method": "CARD", "amount": 30000}])))
    session.add(_ev("billing.interim_paid", "R-SAME", _PAID))
    await session.flush()
    await consumer.consume(session)
    assert await _status(session, "R-REPLAY") == "DEAD"
    assert await _status(session, "R-SAME") == "APPLIED"
    row = await session.scalar(select(InterimBilling).where(InterimBilling.interim_billing_id == "IB-9"))
    assert row.payments is None                       # 거부된 재전송은 원장 줄도 바꾸지 않음


async def test_refund_without_paid_voucher_records_only(session, monkeypatch):
    """MED3 — 게이트 OFF 때 받은 수납의 환불은 게이트 ON이어도 역분개하지 않는다(1131·현금 보호)."""
    _gate(monkeypatch, on=False)
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    await session.flush()
    await consumer.consume(session)
    _gate(monkeypatch, on=True)
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-G", dict(
        _PAID, refund={"refundId": "RF-G", "amount": 5000, "method": "CASH"})))
    await session.flush()
    await consumer.consume(session)
    assert await _status(session, "interim:refund:RF-G") == "APPLIED"
    assert await _lines(session, "interim:refund:RF-G") is None
    assert await session.scalar(select(func.count()).select_from(InterimBillingRefund)) == 1


async def test_paid_line_nonpositive_or_non_object_dead(session):
    """LOW5·6 — 0·음수 원장 줄(환불은 별도 이벤트)·객체 아닌 원소는 즉시 DEAD."""
    session.add(_ev("billing.interim_paid", "R-NEG", dict(_PAID, payments=[
        {"method": "CARD", "amount": 60000}, {"method": "CARD", "amount": -30000}])))
    session.add(_ev("billing.interim_paid", "R-STR", dict(_PAID, interimBillingId="IB-8", payments=["CASH"])))
    await session.flush()
    await consumer.consume(session)
    assert await _status(session, "R-NEG") == "DEAD"
    assert await _status(session, "R-STR") == "DEAD"


async def test_refund_same_id_different_amount_dead(session):
    """LOW8 — 같은 refundId가 다른 금액으로 오면 '이미 반영'으로 삼키지 않는다."""
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", _PAID))
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-D", dict(
        _PAID, refund={"refundId": "RF-D", "amount": 5000, "method": "CASH"})))
    await session.flush()
    await consumer.consume(session)
    session.add(_ev("billing.interim_refunded", "R-DIFF", dict(
        _PAID, refund={"refundId": "RF-D", "amount": 7000, "method": "CASH"})))
    await session.flush()
    await consumer.consume(session)
    assert await _status(session, "R-DIFF") == "DEAD"


async def test_prepaid_legacy_interim_without_period_uses_paid_at(session):
    """LOW7 — 기간 칸이 없는 구 이벤트는 수납 시각으로 이번 입원 여부를 가린다."""
    from erp.regulatory.models import FeeMaster
    session.add(FeeMaster(code="AA100", code_type="TARIFF", name="처치료", unit_price=50_000,
                          valid_from=date(2026, 1, 1)))
    await _billing_line(session, {"lineId": "L-LG", "chartNo": "HR-0009", "code": "AA100",
                                  "encounterType": "INPATIENT", "admittedOn": "2026-10-19",
                                  "treatedOn": "2026-10-20"}, "billing.line_recorded", {})
    session.add(_ev("billing.interim_paid", "interim:paid:IB-LG", dict(
        _PAID, interimBillingId="IB-LG", periodStart=None, periodEnd=None)))
    await session.flush()
    await consumer.consume(session)
    inv = await BillingService().invoice(session, chart_no="HR-0009")
    assert inv["prepaid"] == 30_000


# ── HIS 답신(v4.117.0·2026-10-22) 반영 ─────────────────────────────────────

async def test_refund_order_wait_does_not_consume_retries_then_dead_after_72h(session):
    """HIS엔 수납 전송 성공 뒤 환불을 보내는 장치가 없다 — 선도착 환불은 재시도 횟수를 쓰지 않고 기다리다 72시간 뒤 DEAD."""
    from datetime import datetime, timedelta

    from erp.common.kst import KST
    session.add(_ev("billing.interim_refunded", "interim:refund:RF-W", dict(
        _PAID, refund={"refundId": "RF-W", "amount": 5000, "method": "CASH"})))
    await session.flush()
    ev = await session.scalar(select(HisInboundEvent).where(HisInboundEvent.source_ref == "interim:refund:RF-W"))
    for _ in range(7):                                # 일반 재시도 한도(5)를 넘겨도 DEAD가 아니다
        ev.next_retry_at = None
        await session.flush()
        await consumer.consume(session)
    assert ev.status == "FAILED" and ev.retry_count == 0 and ev.next_retry_at is not None
    ev.received_at = datetime.now(KST).replace(tzinfo=None) - timedelta(hours=73)
    ev.next_retry_at = None
    await session.flush()
    await consumer.consume(session)
    assert ev.status == "DEAD" and "72시간" in ev.error


async def test_his_v4117_refund_payload_applies(session, monkeypatch):
    """HIS 답신 §3 본문 그대로(payments 없음·refundedAt UTC) — 환불 원장·역분개(KST 날짜)."""
    _gate(monkeypatch)
    session.add(_ev("billing.interim_paid", "interim:paid:IB-9", dict(_PAID, payments=[{"method": "CASH", "amount": 30000}])))
    body = dict(_PAID, refund={"refundId": "8c1f-uuid", "amount": 10000, "method": "CASH", "reason": "조기 퇴원 차액",
                               "refundedBy": "u-9", "refundedAt": "2026-10-22T16:00:00.000Z"})
    session.add(_ev("billing.interim_refunded", "interim:refund:8c1f-uuid", body))
    await session.flush()
    await consumer.consume(session)
    assert await _lines(session, "interim:refund:8c1f-uuid") == {("1131", "DEBIT"): 10000, ("1110", "CREDIT"): 10000}
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == "interim:refund:8c1f-uuid"))
    assert v.voucher_on == date(2026, 10, 23)        # 16:00Z = 10-23 01:00 KST
    rf = await session.scalar(select(InterimBillingRefund).where(InterimBillingRefund.refund_id == "8c1f-uuid"))
    assert rf.reason == "조기 퇴원 차액" and rf.refunded_by == "u-9"
