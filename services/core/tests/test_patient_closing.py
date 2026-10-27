"""Patient — 환자 최소화·blind index 검색·일마감→전표(F1)·마감 후 정정(FR-54)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Voucher
from erp.patient.service import ClosingService, PatientService, PaymentService, ReceptionService

_p, _r, _pay, _close = PatientService(), ReceptionService(), PaymentService(), ClosingService()
D = date(2026, 6, 7)


async def _setup_payment(session, amount=50_000):
    patient = await _p.create(session, chart_no="C-1001", name="홍환자",
                              birth_date=date(1980, 5, 5), phone="010-9876-5432")
    rec = await _r.receive(session, patient_id=patient.id, dept_code="IM",
                           doctor_id=7, received_on=D)
    return await _pay.pay(session, reception_id=rec.id, amount=amount,
                          method="CARD", paid_on=D)


async def test_tellers_summary_groups_by_operator(session):
    """수납원별 마감 — 입력자별 수납/환불/현금시재 분리·HIS(자동) 분리."""
    from erp.patient.models import Refund
    p = await _p.create(session, chart_no="C-9001", name="박환자", birth_date=date(1985, 1, 1))
    rec = await _r.receive(session, patient_id=p.id, dept_code="IM", doctor_id=1, received_on=D)
    # 수납원 1: 현금 30000, 수납원 2: 카드 50000, HIS(자동, created_by=None): 현금 10000
    await _pay.pay(session, reception_id=rec.id, amount=30_000, method="CASH",
                   paid_on=D, created_by=1)
    await _pay.pay(session, reception_id=rec.id, amount=50_000, method="CARD",
                   paid_on=D, created_by=2)
    await _pay.pay(session, reception_id=rec.id, amount=10_000, method="CASH", paid_on=D)
    # 수납원 1이 현금 5000 환불
    session.add(Refund(chart_no="C-9001", amount=5_000, method="CASH", reason="과오납",
                       refunded_on=D, created_by=1))
    await session.flush()
    out = await _close.tellers_summary(session, D)
    by_id = {t["teller_id"]: t for t in out["tellers"]}
    assert by_id[1]["cash"] == 30_000 and by_id[1]["net_cash"] == 25_000   # 30000 − 5000 환불
    assert by_id[2]["card"] == 50_000 and by_id[2]["net_cash"] == 0        # 카드만
    assert by_id[None]["teller"] == "HIS(자동)" and by_id[None]["cash"] == 10_000
    assert out["total"] == 90_000


async def test_patient_minimal_fields_and_blind_search(session):
    """FR-33 4필드 + 전화번호 전체 일치 검색 (blind index)."""
    p = await _p.create(session, chart_no="C-2001", name="김환자",
                        birth_date=date(1990, 1, 1), phone="010-1111-2222")
    assert p.phone_enc is not None and p.phone_bidx is not None
    found = await _p.search(session, phone="01011112222")  # 형식 달라도 정규화 일치
    assert [x.id for x in found] == [p.id]
    assert await _p.search(session, phone="010-1111-2229") == []


async def test_reception_updates_last_visit(session):
    p = await _p.create(session, chart_no="C-3001", name="이환자",
                        birth_date=date(1975, 3, 3))
    await _r.receive(session, patient_id=p.id, dept_code="OS", doctor_id=1, received_on=D)
    assert p.last_visit_on == D  # 보존연한 기산


async def test_close_day_creates_voucher_f1(session):
    """E2E ① 단위 버전 — 수납→마감→전표 (단일 Tx)."""
    await _setup_payment(session, amount=50_000)
    closing = await _close.close_day(session, closing_on=D, actor=1)
    assert closing.total_amount == 50_000 and closing.voucher_ref
    voucher = (await session.execute(select(Voucher))).scalar_one()
    assert voucher.source_ref == f"closing-{D.isoformat()}-1"


async def test_payment_on_closed_day_rejected(session):
    """마감 후 수납 차단 — amend 경로 강제 (FR-54)."""
    pay = await _setup_payment(session)
    await _close.close_day(session, closing_on=D, actor=1)
    with pytest.raises(ValidationError):
        await _pay.pay(session, reception_id=pay.reception_id, amount=10_000,
                       method="CASH", paid_on=D)


async def test_double_close_rejected(session):
    await _setup_payment(session)
    await _close.close_day(session, closing_on=D, actor=1)
    with pytest.raises(ValidationError):
        await _close.close_day(session, closing_on=D, actor=1)


async def test_close_voucher_splits_by_method(session):
    """수단별 분개 — CARD→카드미수(1132), CASH/TRANSFER→현금(1110), 대변 외래수익(4110)."""
    from erp.finance.models import VoucherLine
    p = await _p.create(session, chart_no="C-1500", name="최환자", birth_date=date(1970, 1, 1))
    for method, amt in [("CARD", 50_000), ("CASH", 20_000), ("TRANSFER", 10_000)]:
        rec = await _r.receive(session, patient_id=p.id, dept_code="IM", doctor_id=7, received_on=D)
        await _pay.pay(session, reception_id=rec.id, amount=amt, method=method, paid_on=D)
    closing = await _close.close_day(session, closing_on=D, actor=1)
    vid = int(closing.voucher_ref.removeprefix("voucher-"))
    lines = {(ln.account_code, ln.side): float(ln.amount) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == vid))).scalars()}
    assert lines[("1132", "DEBIT")] == 50_000        # 카드미수
    assert lines[("1110", "DEBIT")] == 30_000        # 현금 = CASH 20k + TRANSFER 10k
    assert lines[("4110", "CREDIT")] == 80_000       # 외래수익 = 합계
    # 차변 합 = 대변 합 (정합)
    deb = sum(v for (a, s), v in lines.items() if s == "DEBIT")
    assert deb == lines[("4110", "CREDIT")] == 80_000


async def test_close_cash_only_single_debit(session):
    """현금만이면 카드미수 라인 없이 현금 단일 차변."""
    from erp.finance.models import VoucherLine
    await _setup_payment(session, amount=50_000)     # _setup_payment은 CARD
    # CARD뿐 → 카드미수만
    closing = await _close.close_day(session, closing_on=D, actor=1)
    vid = int(closing.voucher_ref.removeprefix("voucher-"))
    sides = {(ln.account_code, ln.side) for ln in (await session.execute(
        select(VoucherLine).where(VoucherLine.voucher_id == vid))).scalars()}
    assert ("1132", "DEBIT") in sides and ("1110", "DEBIT") not in sides


async def test_preview_before_close(session):
    """마감 전 일계 미리보기 — 합계·건수·수단별·미마감."""
    await _setup_payment(session, amount=50_000)            # CARD
    rec2 = await _r.receive(session, patient_id=(await _p.create(
        session, chart_no="C-1002", name="박환자", birth_date=date(1985, 2, 2))).id,
        dept_code="IM", doctor_id=7, received_on=D)
    await _pay.pay(session, reception_id=rec2.id, amount=20_000, method="CASH", paid_on=D)
    pv = await _close.preview(session, D)
    assert pv["total"] == 70_000 and pv["count"] == 2 and pv["already_closed"] is False
    by = {m["method"]: m["amount"] for m in pv["by_method"]}
    assert by == {"CARD": 50_000, "CASH": 20_000}


async def test_preview_reflects_closed_state(session):
    await _setup_payment(session, amount=50_000)
    await _close.close_day(session, closing_on=D, actor=1)
    assert (await _close.preview(session, D))["already_closed"] is True


async def test_history_lists_and_month_filters(session):
    await _setup_payment(session, amount=50_000)
    await _close.close_day(session, closing_on=D, actor=1)
    hist = await _close.history(session)
    assert len(hist) == 1 and hist[0]["closing_on"] == D.isoformat()
    assert hist[0]["seq"] == 1 and hist[0]["status"] == "CLOSED" and hist[0]["voucher_ref"]
    # 월 필터 — 해당 월 포함, 타 월 제외
    assert len(await _close.history(session, yyyymm="2026-06")) == 1
    assert await _close.history(session, yyyymm="2026-05") == []
    with pytest.raises(ValidationError):
        await _close.history(session, yyyymm="2026/06")


async def test_history_amend_shows_both_seqs(session):
    pay = await _setup_payment(session, amount=50_000)
    await _close.close_day(session, closing_on=D, actor=1)
    await _close.amend(session, closing_on=D, actor=1, reason="정정",
                       correction_payments=[{"cancel_payment_id": pay.id, "new_amount": 30_000}])
    hist = await _close.history(session)        # 최신순 → seq2(CLOSED) 먼저, seq1(AMENDED)
    assert [(h["seq"], h["status"]) for h in hist] == [(2, "CLOSED"), (1, "AMENDED")]


async def _pay_by(session, creator, amount=50_000, method="CARD", chart="C-4E"):
    p = await _p.create(session, chart_no=f"{chart}-{creator}-{amount}",
                        name="사환자", birth_date=date(1980, 1, 1))
    rec = await _r.receive(session, patient_id=p.id, dept_code="IM", doctor_id=7, received_on=D)
    return await _pay.pay(session, reception_id=rec.id, amount=amount, method=method,
                          paid_on=D, created_by=creator)


async def test_4eyes_blocks_self_close(session):
    """직무분리 — 본인이 100% 입력한 수납만 있으면 마감 차단."""
    await _pay_by(session, creator=1)
    with pytest.raises(ValidationError) as ei:
        await _close.close_day(session, closing_on=D, actor=1)
    assert ei.value.details.get("reason") == "self_close"


async def test_4eyes_force_allows_with_audit(session):
    """force(allow_self_close)면 강제 마감 — 전표 생성·감사 self_close 기록."""
    from erp.audit.models import AuditLog
    await _pay_by(session, creator=1)
    closing = await _close.close_day(session, closing_on=D, actor=1, allow_self_close=True)
    assert closing.voucher_ref
    log = (await session.execute(select(AuditLog).where(
        AuditLog.action == "CLOSE"))).scalars().all()[-1]
    assert log.after["self_close"] is True


async def test_4eyes_satisfied_by_other_or_his(session):
    """타 담당자 입력 또는 HIS 인입(created_by None)이 섞이면 4-eyes 충족 → 차단 안 함."""
    await _pay_by(session, creator=1, amount=50_000)
    await _pay_by(session, creator=2, amount=10_000)         # 다른 담당자
    closing = await _close.close_day(session, closing_on=D, actor=1)   # 차단 안 됨
    assert closing.total_amount == 60_000


async def test_preview_self_closing_flag(session):
    await _pay_by(session, creator=1)
    assert (await _close.preview(session, D, viewer=1))["self_closing"] is True
    assert (await _close.preview(session, D, viewer=2))["self_closing"] is False   # 타인은 마감 가능
    assert (await _close.preview(session, D))["self_closing"] is False             # viewer 없음


async def test_partial_unique_blocks_duplicate_closed(session):
    """동일 일자 CLOSED 2건은 부분 유니크로 차단 — 동시 마감(TOCTOU) 이중계상 방지(전체검토 원무)."""
    from sqlalchemy.exc import IntegrityError

    from erp.patient.models import DailyClosing
    session.add(DailyClosing(closing_on=D, seq=1, total_amount=0, payment_count=0, closed_by=1))
    await session.flush()
    session.add(DailyClosing(closing_on=D, seq=2, total_amount=0, payment_count=0, closed_by=1))
    with pytest.raises(IntegrityError):
        await session.flush()


async def test_amend_rejects_cross_date_cancel(session):
    """정정 취소 대상은 해당 마감일 수납만 — 타 일자 수납 취소 차단(전체검토 원무)."""
    await _setup_payment(session, amount=50_000)          # D 수납
    await _close.close_day(session, closing_on=D, actor=1)
    other = date(2026, 6, 8)                              # 다른 일자 수납
    p2 = await _p.create(session, chart_no="C-1002", name="타환자",
                         birth_date=date(1985, 1, 1), phone="010-0000-0000")
    rec2 = await _r.receive(session, patient_id=p2.id, dept_code="IM",
                            doctor_id=7, received_on=other)
    pay2 = await _pay.pay(session, reception_id=rec2.id, amount=10_000,
                          method="CARD", paid_on=other)
    with pytest.raises(ValidationError):
        await _close.amend(session, closing_on=D, actor=1, reason="x",
                           correction_payments=[{"cancel_payment_id": pay2.id, "new_amount": 5_000}])


async def test_amend_reverses_and_recloses(session):
    """FR-54 — 역분개 + 정정 수납 + 재마감 (원 마감 보존)."""
    pay = await _setup_payment(session, amount=50_000)
    original = await _close.close_day(session, closing_on=D, actor=1)

    reclosing = await _close.amend(
        session, closing_on=D, actor=1, reason="금액 오입력",
        correction_payments=[{"cancel_payment_id": pay.id, "new_amount": 30_000}])

    assert original.status == "AMENDED" and reclosing.seq == 2
    assert float(reclosing.total_amount) == 30_000
    vouchers = (await session.execute(select(Voucher).order_by(Voucher.id))).scalars().all()
    # 원 전표(REVERSED) + 역분개 + 재마감 전표 = 3
    assert len(vouchers) == 3
    assert vouchers[0].status == "REVERSED"
    assert vouchers[2].source_ref == f"closing-{D.isoformat()}-2"


# ── 결제수단 카드 정규화 + 기수납 취소 제외 (심층 재검토 보완) ──────────
def test_revenue_lines_card_variants_to_card_ar():
    """카드 변형(CREDIT/카드 등)도 카드미수(1132)로 — 현금 오분류 방지."""
    from erp.patient.service import _revenue_lines
    lines, total = _revenue_lines({"CREDITCARD": 100_000, "카드": 20_000, "CASH": 50_000})
    by = {ln.account_code: (ln.side, ln.amount) for ln in lines}
    assert by["1132"] == ("DEBIT", 120_000)   # 카드 변형 합 → 카드미수
    assert by["1110"] == ("DEBIT", 50_000)    # 현금
    assert total == 170_000


async def test_prepaid_excludes_canceled_not_double_subtract(session):
    """기수납 — 취소(CANCELED)는 이미 PAID서 빠짐, 추가 차감 안 함(환자 과다청구 방지)."""
    from erp.patient.billing_service import BillingService
    from erp.patient.models import Patient, Payment, Reception
    p = Patient(chart_no="C-PP", name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    rc = Reception(patient_id=p.id, dept_code="IM", received_on=D, source="HIS")
    session.add(rc)
    await session.flush()
    # 원 수납 100k가 정정으로 취소(CANCELED)되고 30k 재수납(PAID) — amend 형상
    session.add_all([
        Payment(reception_id=rc.id, amount=100_000, method="CASH", status="CANCELED", paid_on=D),
        Payment(reception_id=rc.id, amount=30_000, method="CASH", status="PAID", paid_on=D)])
    await session.flush()
    prepaid = await BillingService()._prepaid(session, chart_no="C-PP", since=None)
    assert prepaid == 30_000     # PAID만(취소 이중차감 시 -70,000이던 버그)


async def test_closing_day_lock_noop_on_sqlite_and_serialized_flow(session):
    """일자 마감 직렬화 락 — sqlite no-op(에러 없음)이며 pay→close→pay(마감후) 정합 유지."""
    from erp.patient.service import _lock_closing_day
    await _lock_closing_day(session, D)   # sqlite no-op — 예외 없이 통과
    pay = await _setup_payment(session, amount=30_000)
    await _close.close_day(session, closing_on=D, actor=999)   # 타 담당자 마감
    # 마감 후 동일 일자 수납은 직렬화 후 마감상태 확정 검사로 차단(FR-54)
    with pytest.raises(ValidationError, match="마감"):
        await _pay.pay(session, reception_id=pay.reception_id, amount=10_000,
                       method="CASH", paid_on=D)


async def test_pay_tags_care_month_from_reception(session):
    """수납 시 진료월(care_month)을 접수(received_on) 월로 태깅 — 기수납·미수 귀속 정합."""
    from erp.patient.models import Patient, Reception
    p = Patient(chart_no="C-CM", name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    rc = Reception(patient_id=p.id, dept_code="IM", received_on=date(2026, 6, 15), source="HIS")
    session.add(rc)
    await session.flush()
    pay = await _pay.pay(session, reception_id=rc.id, amount=10_000, method="CASH",
                         paid_on=date(2026, 6, 20))   # 수납일 다른 달이어도 진료월=접수월
    assert pay.care_month == "2026-06"


async def test_payment_duplicate_rejected(session):
    """서버 멱등 — 동일 접수·금액·수단·수납일 재수납은 중복(더블클릭/재시도)으로 차단(과수납 방지)."""
    import pytest

    from erp.errors import ValidationError
    patient = await _p.create(session, chart_no="C-DUP", name="중복환자",
                              birth_date=date(1980, 5, 5), phone="010-1111-2222")
    rec = await _r.receive(session, patient_id=patient.id, dept_code="IM",
                           doctor_id=7, received_on=D)
    await _pay.pay(session, reception_id=rec.id, amount=50_000, method="CARD", paid_on=D)
    with pytest.raises(ValidationError, match="중복 수납"):
        await _pay.pay(session, reception_id=rec.id, amount=50_000, method="CARD", paid_on=D)
    # 다른 금액·수단은 통과(부분/추가 수납 정상)
    await _pay.pay(session, reception_id=rec.id, amount=30_000, method="CASH", paid_on=D)
