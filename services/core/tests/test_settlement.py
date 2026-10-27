"""채권/채무·자금관리 (FR-09 확장) — 수금/지급·aging·자금수지·재무제표 반영."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.finance.models import Account
from erp.finance.settlement_service import SettlementService
from erp.finance.statements import StatementService

svc = SettlementService()
ss = StatementService()


async def _accounts(session):
    session.add_all([
        Account(code="1110", name="현금성자산", kind="ASSET"),
        Account(code="1130", name="미수금", kind="ASSET"),
        Account(code="2110", name="미지급금", kind="LIABILITY"),
        Account(code="4110", name="외래수익", kind="REVENUE"),
        Account(code="5110", name="진료비용", kind="EXPENSE"),
    ])
    await session.flush()


async def test_receivable_partial_then_full_collect(session):
    await _accounts(session)
    r = await svc.record_receivable(session, party="국민건강보험공단", amount=1000000,
                                    billed_on=date(2026, 1, 10), due_on=date(2026, 2, 10))
    assert r.status == "OUTSTANDING"
    r = await svc.collect(session, receivable_id=r.id, amount=400000, on=date(2026, 1, 20))
    assert r.status == "PARTIAL" and float(r.collected_amount) == 400000
    r = await svc.collect(session, receivable_id=r.id, amount=600000, on=date(2026, 2, 1))
    assert r.status == "COLLECTED"


async def test_collect_over_outstanding_raises(session):
    await _accounts(session)
    r = await svc.record_receivable(session, party="A", amount=100000,
                                    billed_on=date(2026, 1, 1), due_on=date(2026, 2, 1))
    with pytest.raises(ValidationError, match="초과"):
        await svc.collect(session, receivable_id=r.id, amount=200000, on=date(2026, 1, 15))


async def test_payable_pay(session):
    await _accounts(session)
    p = await svc.record_payable(session, party="대한약품", amount=500000,
                                 incurred_on=date(2026, 1, 1), due_on=date(2026, 2, 1))
    p = await svc.pay(session, payable_id=p.id, amount=500000, on=date(2026, 1, 25))
    assert p.status == "PAID"


async def test_aging_buckets(session):
    await _accounts(session)
    await svc.record_receivable(session, party="A", amount=100000,
                                billed_on=date(2026, 1, 1), due_on=date(2026, 3, 1))   # 미도래
    await svc.record_receivable(session, party="B", amount=200000,
                                billed_on=date(2025, 12, 1), due_on=date(2026, 1, 15))  # 17일 경과
    ag = await svc.aging(session, as_of=date(2026, 2, 1), kind="AR")
    assert ag["buckets"]["current"] == 100000      # 만기 미도래
    assert ag["buckets"]["1-30"] == 200000         # 17일 연체
    assert ag["total"] == 300000


async def test_cash_flow(session):
    await _accounts(session)
    r = await svc.record_receivable(session, party="A", amount=500000,
                                    billed_on=date(2026, 1, 1), due_on=date(2026, 2, 1))
    await svc.collect(session, receivable_id=r.id, amount=500000, on=date(2026, 1, 15))
    p = await svc.record_payable(session, party="공급사", amount=200000,
                                 incurred_on=date(2026, 1, 1), due_on=date(2026, 2, 1))
    await svc.pay(session, payable_id=p.id, amount=200000, on=date(2026, 1, 20))
    cf = await svc.cash_flow(session, since=date(2026, 1, 1), until=date(2026, 1, 31))
    assert cf["inflow"] == 500000 and cf["outflow"] == 200000 and cf["net"] == 300000


async def test_receivable_shows_in_balance_sheet(session):
    await _accounts(session)
    await svc.record_receivable(session, party="공단", amount=1000000,
                                billed_on=date(2026, 1, 1), due_on=date(2026, 2, 1))
    bs = await ss.balance_sheet(session, as_of=date(2026, 1, 31))
    ar = next(a for a in bs["assets"] if a["account_code"] == "1130")
    assert ar["amount"] == 1000000        # 미수금 자산 계상


async def test_collect_seq_refs_unique_per_partial(session):
    """C1/C2 회귀: 분할 수금이 잔액을 정확히 차감하고 채번(seq)이 1,2,3으로 유일 — 행 잠금 적용 후."""
    from sqlalchemy import select

    from erp.finance.models import Voucher
    await _accounts(session)
    r = await svc.record_receivable(session, party="공단", amount=900000,
                                    billed_on=date(2026, 1, 1), due_on=date(2026, 3, 1))
    for amt in (300000, 300000, 300000):
        r = await svc.collect(session, receivable_id=r.id, amount=amt, on=date(2026, 1, 10))
    assert r.status == "COLLECTED" and float(r.collected_amount) == 900000
    refs = (await session.execute(
        select(Voucher.source_ref).where(
            Voucher.source_module == "ar",
            Voucher.source_ref.like(f"ar-collect-{r.id}-%")))).scalars().all()
    assert sorted(refs) == [f"ar-collect-{r.id}-{i}" for i in (1, 2, 3)]   # 채번 충돌 없음


async def test_aging_reconstruct_point_in_time_stable(session):
    """as_of 재구성 — 과거 월말 잔액이 이후 수금과 무관하게 안정(상태기반 소급 오염 해소).

    1월 발생 채권을 2월에 수금해도, 1월말 as_of 재구성은 미수 100만원을 그대로 보여야 함
    (현시점 상태 방식은 이 건이 사라져 0이 됨 → 재구성이 이를 교정)."""
    await _accounts(session)
    r = await svc.record_receivable(session, party="공단", amount=1_000_000,
                                    billed_on=date(2026, 1, 10), due_on=date(2026, 1, 25))
    # 2월에 전액 수금
    await svc.collect(session, receivable_id=r.id, amount=1_000_000, on=date(2026, 2, 5))

    # 1월말 재구성 — 수금 전이므로 미수 100만 유지
    jan = await svc.aging(session, as_of=date(2026, 1, 31), kind="AR", reconstruct=True)
    assert jan["total"] == 1_000_000
    # 2월말 재구성 — 수금 완료로 0
    feb = await svc.aging(session, as_of=date(2026, 2, 28), kind="AR", reconstruct=True)
    assert feb["total"] == 0.0
    # 현시점 상태(reconstruct=False)는 수금 완료라 이미 0 — 1월 스냅샷을 소급 오염하던 방식
    now_state = await svc.aging(session, as_of=date(2026, 1, 31), kind="AR")
    assert now_state["total"] == 0.0    # 잘못된 소급값(재구성이 이를 1,000,000으로 교정)


async def test_aging_reconstruct_patient_ar_via_registered_hook(session):
    """환자 본인부담(1131) 채권도 시점 재구성 정확 — 회수(CR1131)는 일마감 일자단위 집계라
    receivable별 GL 추적 불가. patient가 등록한 재구성기가 Payment(paid_on≤as_of)로 복원(의존성 역전).
    기본 1130 ar-collect 방식이면 환자수납을 못 봐 미수 과다(대손 과다충당)였다."""
    import erp.patient  # noqa: F401 — import 시 재구성기 등록 트리거

    from erp.finance.models import Receivable
    from erp.patient.models import Patient, Payment, Reception
    await _accounts(session)
    pat = Patient(chart_no="CHART-PA", name="환자", birth_date=date(1990, 1, 1))
    session.add(pat)
    await session.flush()
    rc = Reception(patient_id=pat.id, dept_code="IM", received_on=date(2026, 6, 15), source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Receivable(
        party="CHART-PA", description="환자 본인부담 2026-06", amount=100_000, collected_amount=0,
        billed_on=date(2026, 6, 30), due_on=date(2026, 6, 30), status="OUTSTANDING",
        source_module="patient", source_ref="patient-ar:CHART-PA:2026-06"))
    session.add(Payment(reception_id=rc.id, amount=60_000, method="CASH", paid_on=date(2026, 7, 5),
                        status="PAID", care_month="2026-06"))
    await session.flush()
    # 6월말 재구성 — 수납(7/5) 전이므로 미수 100,000
    jun = await svc.aging(session, as_of=date(2026, 6, 30), kind="AR", reconstruct=True)
    assert jun["total"] == 100_000
    # 7월말 재구성 — 수납 60,000 반영 → 잔여미수 40,000 (기본 1130 방식이면 100,000 그대로 과다)
    jul = await svc.aging(session, as_of=date(2026, 7, 31), kind="AR", reconstruct=True)
    assert jul["total"] == 40_000


async def test_aging_reconstruct_excludes_future_billed(session):
    """as_of 이후 발생분은 그 달 aging에서 제외(미래 채권 누출 차단)."""
    await _accounts(session)
    await svc.record_receivable(session, party="A", amount=300_000,
                                billed_on=date(2026, 1, 5), due_on=date(2026, 2, 5))
    await svc.record_receivable(session, party="B(미래)", amount=500_000,
                                billed_on=date(2026, 3, 1), due_on=date(2026, 4, 1))
    jan = await svc.aging(session, as_of=date(2026, 1, 31), kind="AR", reconstruct=True)
    assert jan["total"] == 300_000        # 3월 발생분 B 제외


async def test_aging_reconstruct_partial_collection(session):
    """부분 수금 — as_of 이하 수금분만 차감."""
    await _accounts(session)
    r = await svc.record_receivable(session, party="공단", amount=1_000_000,
                                    billed_on=date(2026, 1, 10), due_on=date(2026, 2, 10))
    await svc.collect(session, receivable_id=r.id, amount=400_000, on=date(2026, 1, 20))
    await svc.collect(session, receivable_id=r.id, amount=600_000, on=date(2026, 2, 15))
    jan = await svc.aging(session, as_of=date(2026, 1, 31), kind="AR", reconstruct=True)
    assert jan["total"] == 600_000        # 1월 수금 40만 차감 → 60만 잔여


async def test_aging_reconstruct_ap_payment(session):
    """AP도 지급 시점 기준 재구성."""
    await _accounts(session)
    p = await svc.record_payable(session, party="공급사", amount=800_000,
                                 incurred_on=date(2026, 1, 5), due_on=date(2026, 2, 5))
    await svc.pay(session, payable_id=p.id, amount=800_000, on=date(2026, 2, 10))
    jan = await svc.aging(session, as_of=date(2026, 1, 31), kind="AP", reconstruct=True)
    assert jan["total"] == 800_000        # 1월말엔 미지급
    feb = await svc.aging(session, as_of=date(2026, 2, 28), kind="AP", reconstruct=True)
    assert feb["total"] == 0.0


async def test_overview_reconstruct_threads_to_ar_balance(session):
    """overview(reconstruct=True)가 미수/미지급잔액을 as_of 재구성으로 산출(마트 경로).

    1월 발생 채권을 2월 수금 → 1월 overview 재구성은 미수 유지(현시점 방식은 0으로 소급 오염)."""
    from erp.analytics.service import AnalyticsService
    await _accounts(session)
    r = await svc.record_receivable(session, party="공단", amount=700_000,
                                    billed_on=date(2026, 1, 8), due_on=date(2026, 1, 25))
    await svc.collect(session, receivable_id=r.id, amount=700_000, on=date(2026, 2, 3))
    await session.flush()
    an = AnalyticsService()
    recon = await an.overview(session, yyyymm="2026-01", reconstruct=True)
    assert recon["재무"]["미수금잔액"] == 700_000       # 1월말 기준 미수 유지
    now_state = await an.overview(session, yyyymm="2026-01")   # 기본 현시점
    assert now_state["재무"]["미수금잔액"] == 0.0        # 소급 오염(재구성이 교정)


async def test_cash_flow_includes_fx_deposit(session):
    """자금수지 현금계정 일반화 — 법인 외화예금(1115)도 현금으로 집계(단일 1110 해소·BI 마트 외화 반영)."""
    from erp.finance.models import Account, CorporateAccount
    from erp.finance.service import VoucherLineDTO
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4110", name="수익", kind="REVENUE"),
        CorporateAccount(alias="신한 USD", bank_name="신한", account_no="X",
                         currency="USD", gl_account_code="1115", is_active=True),
    ])
    await session.flush()
    # 원화 현금 유입 50k + 외화예금 유입 30k
    await svc._v.create(session, source_module="manual", source_ref="cf-krw",
                        voucher_on=date(2026, 5, 5),
                        lines=[VoucherLineDTO("1110", "DEBIT", 50000),
                               VoucherLineDTO("4110", "CREDIT", 50000)])
    await svc._v.create(session, source_module="manual", source_ref="cf-fx",
                        voucher_on=date(2026, 5, 6),
                        lines=[VoucherLineDTO("1115", "DEBIT", 30000),
                               VoucherLineDTO("4110", "CREDIT", 30000)])
    await session.flush()
    cf = await svc.cash_flow(session, since=date(2026, 5, 1), until=date(2026, 5, 31))
    assert cf["inflow"] == 80000        # 1110(50k) + 1115(30k) — 외화예금 포함
    assert cf["net"] == 80000
