"""환자 본인부담 발생주의(accrual) — 진료시점 미수금 발생·delta 멱등·수금 반영·게이트.

발생: DR 1131 환자미수금 / CR 4110 외래수익(보험분과 일관). 수납: DR 현금·카드 / CR 1131(상계).
config patient_accrual_enabled OFF면 무전기(현행 현금주의).
"""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.config import get_settings
from erp.finance.models import Receivable, Voucher, VoucherLine
from erp.patient.models import BillingLine, Patient, Payment, Reception
from erp.patient.receivable_service import PatientReceivableService
from erp.patient.service import _revenue_lines

svc = PatientReceivableService()


@pytest.fixture
def _accrual_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "patient_accrual_enabled", True, raising=False)


async def _bill(session, *, chart, line_id, unit, count=1, coverage="NONCOVERED",
                treated="2026-06-15"):
    session.add(BillingLine(line_id=line_id, chart_no=chart, code=f"NC{line_id}",
                            count=count, coverage=coverage, encounter_type="OUTPATIENT",
                            treated_on=date.fromisoformat(treated), unit_price_his=unit))
    await session.flush()


async def _gl(session, code: str) -> float:
    """계정 순잔액(차−대)."""
    d, c = (await session.execute(select(
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "DEBIT"), 0),
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "CREDIT"), 0))
        .where(VoucherLine.account_code == code))).one()
    return float(d) - float(c)


async def _vcount(session) -> int:
    return int((await session.execute(select(func.count()).select_from(Voucher))).scalar_one())


# ── _revenue_lines 회계모델 분기(순수) ────────────────────────
def test_revenue_lines_credit_account_by_mode():
    cash_lines, _ = _revenue_lines({"CASH": 30_000}, accrual=False)
    assert ("4110", "CREDIT", 30_000) in [(line.account_code, line.side, line.amount)
                                          for line in cash_lines]
    acc_lines, _ = _revenue_lines({"CASH": 30_000}, accrual=True)
    assert ("1131", "CREDIT", 30_000) in [(line.account_code, line.side, line.amount)
                                          for line in acc_lines]


# ── 발생·delta 멱등 ───────────────────────────────────────────
async def test_accrue_posts_and_is_delta_idempotent(session, _accrual_on):
    await _bill(session, chart="C-AR1", line_id="L1", unit=50_000)   # 비급여 = 본인 100%
    r1 = await svc.accrue(session, chart_no="C-AR1", care_month="2026-06")
    await session.flush()
    assert r1["target"] == 50_000 and r1["delta"] == 50_000
    assert await _gl(session, "1131") == 50_000 and await _gl(session, "4110") == -50_000
    rec = (await session.execute(select(Receivable).where(
        Receivable.source_module == "patient"))).scalar_one()
    assert float(rec.amount) == 50_000 and rec.status == "OUTSTANDING"
    n = await _vcount(session)
    # 재실행 — delta 0(목표 불변), 신규 전표 없음
    r2 = await svc.accrue(session, chart_no="C-AR1", care_month="2026-06")
    assert r2["delta"] == 0 and await _vcount(session) == n


async def test_accrue_increase_posts_only_delta(session, _accrual_on):
    await _bill(session, chart="C-AR2", line_id="L2", unit=50_000)
    await svc.accrue(session, chart_no="C-AR2", care_month="2026-06")
    await _bill(session, chart="C-AR2", line_id="L3", unit=30_000)   # 본인부담 증가 → 80,000
    r = await svc.accrue(session, chart_no="C-AR2", care_month="2026-06")
    await session.flush()
    assert r["target"] == 80_000 and r["delta"] == 30_000
    assert await _gl(session, "1131") == 80_000


# ── 수금 반영·aging ───────────────────────────────────────────
async def test_payment_reflected_in_collected_and_aging(session, _accrual_on):
    from erp.finance.settlement_service import SettlementService
    await _bill(session, chart="C-AR3", line_id="L4", unit=50_000)
    pat = Patient(chart_no="C-AR3", name="환자", birth_date=date(1990, 1, 1))
    session.add(pat)
    await session.flush()
    rcp = Reception(patient_id=pat.id, dept_code="IM", received_on=date(2026, 6, 15), source="HIS")
    session.add(rcp)
    await session.flush()
    session.add(Payment(reception_id=rcp.id, amount=20_000, method="CASH",
                        paid_on=date(2026, 6, 15), status="PAID", care_month="2026-06"))
    await session.flush()
    r = await svc.accrue(session, chart_no="C-AR3", care_month="2026-06")
    await session.flush()
    assert r["collected"] == 20_000 and r["status"] == "PARTIAL"
    aging = await SettlementService().aging(session, as_of=date(2026, 6, 30),
                                            kind="AR", source_module="patient")
    assert aging["total"] == 30_000        # 50,000 − 20,000 수금


# ── 게이트 OFF ────────────────────────────────────────────────
async def test_gate_off_is_noop(session):
    await _bill(session, chart="C-AR4", line_id="L5", unit=50_000)
    r = await svc.accrue(session, chart_no="C-AR4", care_month="2026-06")
    assert r["enabled"] is False
    assert await _vcount(session) == 0
    assert (await session.execute(select(func.count()).select_from(Receivable))).scalar_one() == 0


async def test_accrue_target_return_reposts_delta_gl_consistent(session, _accrual_on):
    """CRITICAL 회귀 — 목표 100→150→100 되돌아와도 보정(-50) 전표가 멱등 충돌로 드롭되지 않고 GL=목표.

    기존: source_ref에 target 인코딩 → `:100` 재사용 충돌 → -50 드롭 → GL 150 vs 미러 100 영구 불일치.
    수정: accrued를 GL서 산출 + source_ref 단조 유니크(vN) → 정상 재전기.
    """
    await _bill(session, chart="C-RT", line_id="RT1", unit=100_000)
    await svc.accrue(session, chart_no="C-RT", care_month="2026-06")     # +100k (v1)
    await _bill(session, chart="C-RT", line_id="RT2", unit=50_000)
    await svc.accrue(session, chart_no="C-RT", care_month="2026-06")     # +50k (v2) → GL 150k
    assert await _gl(session, "1131") == 150_000
    # 라인 취소 → target 100k 복귀(과거값 재도달)
    bl = await session.scalar(select(BillingLine).where(BillingLine.line_id == "RT2"))
    bl.cancelled = True
    await session.flush()
    r3 = await svc.accrue(session, chart_no="C-RT", care_month="2026-06")
    assert r3["delta"] == -50_000
    assert await _gl(session, "1131") == 100_000    # 보정 전기됨(드롭 아님) — GL=목표
    # 재실행 멱등 — 목표 불변이면 추가 전기 없음
    n = await _vcount(session)
    await svc.accrue(session, chart_no="C-RT", care_month="2026-06")
    assert await _vcount(session) == n


async def test_accrue_all_isolates_failing_chart(session, _accrual_on, monkeypatch):
    """배치 오류격리 — accrue_all에서 1개 chart 실패가 전체를 롤백하지 않고 나머지는 정상 전기.

    (마감월 late billing 등으로 VoucherService가 raise해도 야간 잡이 병원 전체 발생주의를
    무기한 막지 않도록 chart별 savepoint 격리)."""
    await _bill(session, chart="C-OK", line_id="OK1", unit=100_000)
    await _bill(session, chart="C-BAD", line_id="BAD1", unit=50_000)
    orig = PatientReceivableService.accrue

    async def _accrue(self, session, *, chart_no, care_month, actor=None):
        if chart_no == "C-BAD":
            raise RuntimeError("마감월 전기 실패 시뮬레이션")
        return await orig(self, session, chart_no=chart_no, care_month=care_month, actor=actor)

    monkeypatch.setattr(PatientReceivableService, "accrue", _accrue)
    r = await svc.accrue_all(session, care_month="2026-06")
    assert r["charts"] == 2 and r["posted"] == 1 and r["failed"] == 1
    # 실패 chart 격리(savepoint 롤백)·정상 chart 전기 유지 — GL에 C-OK 발생분만
    assert await _gl(session, "1131") == 100_000


@pytest.fixture
def _cutover_jun(monkeypatch):
    monkeypatch.setattr(get_settings(), "patient_accrual_enabled", True, raising=False)
    monkeypatch.setattr(get_settings(), "patient_accrual_cutover_month", "2026-06", raising=False)


async def test_accrue_skips_pre_cutover_month(session, _cutover_jun):
    """cutover 가드 — 기준월(2026-06) 이전 진료분은 accrue 제외(현금주의 재인식 이중계상 방지)."""
    await _bill(session, chart="C-PRE", line_id="P1", unit=50_000, treated="2026-05-15")
    r = await svc.accrue(session, chart_no="C-PRE", care_month="2026-05")
    assert r.get("skipped") == "pre_cutover" and await _vcount(session) == 0
    # 기준월 이후는 정상 발생
    await _bill(session, chart="C-PRE", line_id="P2", unit=50_000, treated="2026-06-15")
    r2 = await svc.accrue(session, chart_no="C-PRE", care_month="2026-06")
    assert r2["delta"] == 50_000 and await _gl(session, "1131") == 50_000


async def _pre_payment(session, chart, amount, on):
    p = Patient(chart_no=chart, name="환자", birth_date=date(1990, 1, 1))
    session.add(p)
    await session.flush()
    rc = Reception(patient_id=p.id, dept_code="IM", received_on=on, source="HIS")
    session.add(rc)
    await session.flush()
    session.add(Payment(reception_id=rc.id, amount=amount, method="CASH", status="PAID",
                        paid_on=on, care_month=on.strftime("%Y-%m")))
    await session.flush()


async def test_open_cutover_books_outstanding_and_idempotent(session, _cutover_jun):
    """개통전기 — 이전 진료월 미수납분(본인부담−수납)만 DR1131/CR4110 당기 인식·멱등."""
    await _bill(session, chart="C-OC", line_id="O1", unit=100_000, treated="2026-05-15")  # 본인 100k
    await _pre_payment(session, "C-OC", 30_000, date(2026, 5, 20))                        # 수납 30k
    out = await svc.open_cutover(session, cutover_month="2026-06")
    assert out["opened"] == 1 and out["opened_amount"] == 70_000   # 미수 100k−30k
    assert await _gl(session, "1131") == 70_000 and await _gl(session, "4110") == -70_000
    # 멱등 — 재실행 시 추가 개통 없음
    n = await _vcount(session)
    out2 = await svc.open_cutover(session, cutover_month="2026-06")
    assert out2["opened"] == 0 and await _vcount(session) == n
