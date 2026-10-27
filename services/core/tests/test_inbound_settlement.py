"""입원 정산 이벤트 3종 소비(HIS 요청 2026-07-23·마이그 0224) — 원장 upsert·GL 게이트·대사 경고."""
from sqlalchemy import func, select

from erp.integrations.inbound_consumer import InboundConsumer
from erp.integrations.models import HisInboundEvent
from erp.integrations.target_token import make_target_token
from erp.patient.models import InpatientSettlement, InterimBilling

consumer = InboundConsumer()


def _ev(event_type, ref, payload):
    return HisInboundEvent(domain="finance", event_type=event_type, source_ref=ref,
                           payload=payload, status="NEW")


_INTERIM = {
    "interimBillingId": "IB-1", "admissionId": "ADM-1", "chartNo": "HR-0001",
    "sequenceNo": 1, "periodStart": "2026-07-01", "periodEnd": "2026-07-10",
    "totalAmount": 350000, "insuranceAmount": 280000, "copayAmount": 70000,
    "paidAmount": 0, "paidAt": None, "status": "GENERATED",
}


async def test_interim_recorded_upsert_no_gl_when_gated_off(session):
    """게이트 OFF(기본) — 원장 적재만, 전표 없음."""
    from erp.finance.models import Voucher
    session.add(_ev("billing.interim_recorded", "interim:rec:IB-1", _INTERIM))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1
    row = await session.scalar(select(InterimBilling).where(
        InterimBilling.interim_billing_id == "IB-1"))
    assert row is not None and float(row.copay_amount) == 70000 and row.status == "GENERATED"
    assert await session.scalar(select(Voucher).where(
        Voucher.source_ref == "interim:rec:IB-1")) is None


async def test_interim_gate_split_recorded_no_gl_paid_cash(session, monkeypatch):
    """(가) 확정 — 게이트 ON이어도 interim_recorded는 무GL(매출은 line_recorded 정본)·
    interim_paid는 DR1110/CR1131 현금 유지(재원 중 1131 선수 성격). 멱등."""
    from erp.config import get_settings
    from erp.finance.models import Voucher, VoucherLine
    monkeypatch.setattr(get_settings(), "his_interim_gl_enabled", True, raising=False)
    session.add(_ev("billing.interim_recorded", "interim:rec:IB-1", _INTERIM))
    paid = dict(_INTERIM, paidAmount=70000, paidAt="2026-07-11T09:00:00+09:00", status="PAID")
    session.add(_ev("billing.interim_paid", "interim:paid:IB-1", paid))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 2
    # recorded는 전표 없음(매출 인식은 line_recorded로 이동)
    assert await session.scalar(select(Voucher).where(
        Voucher.source_ref == "interim:rec:IB-1")) is None
    # paid는 현금흐름 전표 유지
    pay = await session.scalar(select(Voucher).where(Voucher.source_ref == "interim:paid:IB-1"))
    plines = {(ln.account_code, ln.side): float(ln.amount) for ln in (await session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == pay.id))).all()}
    assert plines == {("1110", "DEBIT"): 70000, ("1131", "CREDIT"): 70000}
    row = await session.scalar(select(InterimBilling).where(
        InterimBilling.interim_billing_id == "IB-1"))
    assert row.status == "PAID" and float(row.paid_amount) == 70000
    # 재수신 멱등 — 현금 전표 중복 없음
    session.add(_ev("billing.interim_paid", "interim:paid:IB-1:dup", paid))
    await session.flush()
    await consumer.consume(session)
    n = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref == "interim:paid:IB-1"))
    assert n == 1


async def test_discharge_settlement_upsert_replace_and_warning_notification(session):
    """값-멱등 재발행 시 admission 단위 대체 + 경고 시 ADMIN 알림."""
    from erp.common.models import Notification
    from tests._http import make_user
    await make_user(session, user_id=1, role="ADMIN")
    base = {"admissionId": "ADM-9", "chartNo": "HR-0009", "admitDate": "2026-07-01",
            "dischargeDate": "2026-07-12", "days": 12, "totalCharges": 1200000,
            "totalCopay": 240000, "alreadyPaid": 140000, "balanceDue": 100000,
            "reconciliation": {"balanced": True, "overpaid": False, "overpaidAmount": 0,
                               "unpaidInterims": 0, "warnings": []},
            "exportedAt": "2026-07-12T10:00:00+09:00"}
    session.add(_ev("billing.discharge_settlement", "dsettle:ADM-9:240000:140000", base))
    await session.flush()
    await consumer.consume(session)
    row = await session.scalar(select(InpatientSettlement).where(
        InpatientSettlement.admission_id == "ADM-9"))
    assert float(row.balance_due) == 100000 and row.balanced is True
    assert await session.scalar(select(Notification).where(
        Notification.type == "INPATIENT_SETTLEMENT")) is None   # 경고 없음=알림 없음
    # 금액 변경 재발행(새 ref) → 동일 admission 대체 + 미납 경고 알림
    upd = dict(base, alreadyPaid=100000, balanceDue=140000,
               reconciliation={"balanced": False, "overpaid": False, "overpaidAmount": 0,
                               "unpaidInterims": 1, "warnings": ["미수납 중간정산 1건"]})
    session.add(_ev("billing.discharge_settlement", "dsettle:ADM-9:240000:100000", upd))
    await session.flush()
    await consumer.consume(session)
    rows = (await session.scalars(select(InpatientSettlement).where(
        InpatientSettlement.admission_id == "ADM-9"))).all()
    assert len(rows) == 1 and float(rows[0].balance_due) == 140000   # 대체(중복 행 없음)
    noti = await session.scalar(select(Notification).where(
        Notification.type == "INPATIENT_SETTLEMENT"))
    assert noti is not None and "미수납 중간정산 1건" in noti.body


# ── S-3 대상 토큰(발신 헬퍼) ────────────────────────────────
def test_target_token_format_and_hmac():
    import hashlib
    import hmac as hmac_mod
    tok = make_target_token("HR-0001", secret="s3cret", ttl_sec=300, now=1_700_000_000)
    target, exp, mac = tok.split(".")
    assert target == "HR-0001" and int(exp) == 1_700_000_300
    expect = hmac_mod.new(b"s3cret", f"{target}.{exp}".encode(), hashlib.sha256).hexdigest()
    assert mac == expect


def test_target_token_requires_secret(monkeypatch):
    import pytest as _pytest
    from erp.config import get_settings
    monkeypatch.setattr(get_settings(), "his_target_token_secret", None, raising=False)
    with _pytest.raises(ValueError):
        make_target_token("HR-0001")


# ---- 게이트 의미 경계(HIS B-1 오해 재발 방지) ----

async def test_billing_line_posts_no_gl_even_with_interim_gate_on(session, monkeypatch):
    """`his_interim_gl_enabled`는 **수납** 게이트 — 켜도 라인 수신은 수익을 인식하지 않는다.

    HIS B-1에서 "이 게이트만 켜면 입원수익 DR1131/CR4115가 인식된다"고 양측이 오해했다.
    발생 인식은 patient 도메인(ReceivableService.accrue → DR1131/CR4110·게이트 별개)의 몫이고,
    라인 핸들러는 billing_lines 적재만 한다. 이 경계가 무너지면 1131이 양쪽에서 이중 계상된다.
    """
    from erp.config import get_settings
    from erp.finance.models import Voucher, VoucherLine
    from erp.patient.models import BillingLine

    s = get_settings()
    monkeypatch.setattr(s, "his_interim_gl_enabled", True, raising=False)

    session.add(_ev("billing.line_recorded", "line:L-1", {
        "lineId": "L-1", "chartNo": "HR-0001", "code": "AB1A0100", "codeType": "TARIFF",
        "count": 52, "unitPrice": 50000, "amount": 2600000, "copayRate": 0.2,
        "encounterType": "INPATIENT", "admittedOn": "2026-06-02", "treatedOn": "2026-06-12",
        "coverage": "NHI", "cancelled": False}))
    await session.flush()
    out = await consumer.consume(session)
    assert out["applied"] == 1

    line = await session.scalar(select(BillingLine).where(BillingLine.line_id == "L-1"))
    assert line is not None and line.encounter_type == "INPATIENT"
    assert float(line.his_copay_rate) == 0.2      # 라인별 요율 보존(환자 안에서도 요율이 다름)

    # 수익·미수 계정 어디에도 전기가 없어야 한다
    posted = await session.scalar(
        select(func.count(VoucherLine.id)).join(Voucher, Voucher.id == VoucherLine.voucher_id)
        .where(VoucherLine.account_code.in_(("1131", "4110", "4115"))))
    assert posted == 0
