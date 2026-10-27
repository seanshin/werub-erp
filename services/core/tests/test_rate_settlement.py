"""v1.295.0 — 요율 차액 정산(결정=실행). 2026-07 급여가 2025 요율로 확정된 상황을 재현한다.

기록 공제(4.5%·3.545%·12.95%) ↔ 2026 고시(4.75%·3.595%·13.14%) — 보수월액 300만원이면 NP +7,500·HI +1,500·LTC +400 = 9,400.
"""
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from erp.common.models import PendingDecision
from erp.errors import ValidationError
from erp.finance.models import Voucher, VoucherLine
from erp.hr import rate_settlement as rs
from erp.hr.employee_service import PayrollService
from erp.hr.models import Employee, PayrollLine, PayrollRecord, RateSettlement, RateSettlementItem

_pay = PayrollService()
_KEY = "statutory-2026-rates-check"
_OPTS = [{"key": "NEXT_PAYROLL_ADJUST", "label": "다음 급여에서 차액 정산", "recommended": True},
         {"key": "EMPLOYER_ABSORB", "label": "근로자분 차액을 기관이 부담"},
         {"key": "NOT_PAYROLL_SOT", "label": "ERP 급여는 지급 원천이 아님"}]


async def _emp_with_july(session, no: str, *, resigned_on: date | None = None, source: str = "2026-07",
                         confirmed: bool = True) -> Employee:
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP", resigned_on=resigned_on)
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm=source, confirmed_at=datetime.now(UTC) if confirmed else None,
                        paid_on=date(2026, 7, 25))
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=rec.id, item_code="NP", item_kind="DEDUCTION", amount=135_000,
                    calc_basis={"base": 3_000_000, "rate": 0.045}),
        PayrollLine(record_id=rec.id, item_code="HI", item_kind="DEDUCTION", amount=106_350,
                    calc_basis={"base": 3_000_000, "rate": 0.03545}),
        PayrollLine(record_id=rec.id, item_code="LTC", item_kind="DEDUCTION", amount=13_770,
                    calc_basis={"base": 106_350}),
        PayrollLine(record_id=rec.id, item_code="EI", item_kind="DEDUCTION", amount=27_000,
                    calc_basis={"base": 3_000_000})])
    await session.flush()
    return e


async def _decision(session) -> PendingDecision:
    d = PendingDecision(key=_KEY, title="2026-07 차액 처리", question="?", options=_OPTS, category="HR")
    session.add(d)
    await session.flush()
    return d


async def _adj_lines(session, emp_id: int, ym: str) -> dict:
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == emp_id,
                                                           PayrollRecord.yyyymm == ym))
    rows = (await session.scalars(select(PayrollLine).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code.in_(rs.SETTLEMENT_LINE_CODES)))).all()
    return {ln.item_code: int(ln.amount) for ln in rows}


async def _voucher(session, ref: str) -> dict:
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ref))
    assert v is not None
    out = {}
    for ln in await session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == v.id)):
        out[(ln.account_code, ln.side)] = int(ln.amount)
    return out


async def test_next_payroll_adjust_end_to_end(session):
    e = await _emp_with_july(session, "RS-1")
    d = await _decision(session)
    p = await rs.preview(session, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-10")
    assert p["affected"] == 1 and p["deduct_count"] == 1 and p["employee_total"] == 9_400
    assert p["voucher_amount"] == 0                      # 사업주 전기 OFF·퇴사자 없음 → 전표 없음
    out = await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST",
                           target="2026-10", note="인사위원회 10/12 결정")
    assert out["status"] == "ACTIVE" and out["voucher_ref"] is None
    await session.refresh(d)
    assert d.status == "DECIDED" and d.decided_option == "NEXT_PAYROLL_ADJUST" and "정산 #" in d.decided_note

    # 10월 급여 생성 → 소급정산 공제 줄. 재생성·입력값에 섞여 와도 한 번만.
    lines = [{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}]
    await _pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-10", lines=lines)
    assert await _adj_lines(session, e.id, "2026-10") == {"NP_ADJ": 7_500, "HI_ADJ": 1_500, "LTC_ADJ": 400}
    await _pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-10", lines=lines + [
        {"item_code": "NP_ADJ", "item_kind": "DEDUCTION", "amount": 99}])
    assert await _adj_lines(session, e.id, "2026-10") == {"NP_ADJ": 7_500, "HI_ADJ": 1_500, "LTC_ADJ": 400}

    rec = await _pay.confirm(session, actor=1, employee_id=e.id, yyyymm="2026-10")
    item = await session.scalar(select(RateSettlementItem).where(RateSettlementItem.employee_id == e.id))
    assert item.applied_record_id == rec.id
    gl = await _voucher(session, f"payroll:{rec.id}")
    assert gl[("2150", "CREDIT")] == 9_400                # 소급 공제가 예수금(공단 납부)으로
    with pytest.raises(ValidationError, match="취소할 수 없습니다"):
        await rs.cancel(session, actor=1, settlement_id=out["id"], reason="방식 변경")


async def test_execute_attaches_to_already_generated_unconfirmed_payroll(session):
    e = await _emp_with_july(session, "RS-2")
    await _pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-10",
                            lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST",
                     target="2026-10", note="이미 생성분 반영")
    assert sum((await _adj_lines(session, e.id, "2026-10")).values()) == 9_400


async def test_resigned_employee_share_goes_to_employer_voucher(session):
    await _emp_with_july(session, "RS-3A")
    await _emp_with_july(session, "RS-3B", resigned_on=date(2026, 8, 31))
    out = await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST",
                           target="2026-10", note="퇴사자 포함")
    assert out["deduct"] == 1 and out["absorb"] == 1 and out["absorbed_total"] == 9_400
    v = await _voucher(session, out["voucher_ref"])
    assert v == {("5330", "DEBIT"): 9_400, ("2110", "CREDIT"): 9_400}


async def test_confirmed_target_payroll_blocks(session):
    e = await _emp_with_july(session, "RS-4")
    await _pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-09",
                            lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    await _pay.confirm(session, actor=1, employee_id=e.id, yyyymm="2026-09")
    p = await rs.preview(session, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-09")
    assert p["blocked"] and "RS-4" in p["blocked"]
    with pytest.raises(ValidationError, match="이미 확정"):
        await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST",
                         target="2026-09", note="확정된 달")
    with pytest.raises(ValidationError, match="이후여야"):
        await rs.preview(session, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-07")


async def test_employer_absorb_corrects_only_posted_employer_share_then_cancel_reopens(session):
    """사업주분 보정은 원 급여월에 사업주 부담 전표가 실제로 있던 직원만(오늘 설정 무관·검토 MED)."""
    from erp.finance.service import VoucherLineDTO, VoucherService
    a = await _emp_with_july(session, "RS-5A")
    await _emp_with_july(session, "RS-5B")              # 사업주분 전표 없음(당시 설정 OFF)
    rec_a = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == a.id))
    await VoucherService().create(session, source_module="hr.payroll", source_ref=f"payroll-employer:{rec_a.id}",
                                  voucher_on=date(2026, 7, 25), memo="t",
                                  lines=[VoucherLineDTO("5330", "DEBIT", 100), VoucherLineDTO("2110", "CREDIT", 100)])
    d = await _decision(session)
    out = await rs.execute(session, actor=1, source="2026-07", method="EMPLOYER_ABSORB", target="2026-10",
                           note="기관 부담 결정")
    assert out["target_yyyymm"] is None                 # 기관 부담은 공제 월 없음
    v = await _voucher(session, out["voucher_ref"])
    assert v[("5330", "DEBIT")] == 2 * 9_400 + 9_400     # 근로자분 2명 + 사업주분 보정 1명(A만)
    with pytest.raises(ValidationError, match="이미 실행"):
        await rs.execute(session, actor=1, source="2026-07", method="NOT_PAYROLL_SOT", target=None, note="중복 실행")
    await rs.cancel(session, actor=1, settlement_id=out["id"], reason="세무 검토 후 재결정")
    s = await session.get(RateSettlement, out["id"])
    assert s.status == "CANCELED"
    orig = await session.scalar(select(Voucher).where(Voucher.source_ref == out["voucher_ref"]))
    assert orig.status == "REVERSED"
    await session.refresh(d)
    assert d.status == "PENDING" and d.decided_option is None
    again = await rs.execute(session, actor=1, source="2026-07", method="NOT_PAYROLL_SOT", target=None,
                             note="외부 급여가 원천")
    assert again["voucher_ref"] is None


async def test_unconfirmed_source_month_blocks(session):
    """원 급여월 미확정분은 재생성하면 새 요율 — 정산하면 이중 정정(검토 MED)."""
    await _emp_with_july(session, "RS-8", source="2026-05", confirmed=False)
    p = await rs.preview(session, source="2026-05", method="EMPLOYER_ABSORB", target=None)
    assert p["blocked"] and "미확정" in p["blocked"]
    with pytest.raises(ValidationError, match="미확정"):
        await rs.execute(session, actor=1, source="2026-05", method="EMPLOYER_ABSORB", target=None, note="5월 정산")


async def test_two_settlements_cannot_share_target_month(session):
    """같은 공제월에 두 정산 — 직원당 한 정산분만 붙어 나머지가 사라지던 결함(검토 HIGH)."""
    e = await _emp_with_july(session, "RS-9")
    rec6 = PayrollRecord(employee_id=e.id, yyyymm="2026-06", confirmed_at=datetime.now(UTC))
    session.add(rec6)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec6.id, item_code="NP", item_kind="DEDUCTION", amount=135_000,
                    calc_basis={"base": 3_000_000}),
        PayrollLine(record_id=rec6.id, item_code="HI", item_kind="DEDUCTION", amount=106_350,
                    calc_basis={"base": 3_000_000})])
    await session.flush()
    await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-10", note="7월분 정산")
    p = await rs.preview(session, source="2026-06", method="NEXT_PAYROLL_ADJUST", target="2026-10")
    assert p["blocked"] and "2026-07 정산이 이미 2026-10" in p["blocked"]
    out = await rs.execute(session, actor=1, source="2026-06", method="NEXT_PAYROLL_ADJUST", target="2026-11",
                           note="6월분은 11월")
    assert out["status"] == "ACTIVE"


async def test_absorb_remaining_after_resign_post_execution(session, monkeypatch):
    """실행 뒤 퇴사해 공제월 급여가 없는 직원분 — 기관 부담으로 돌리는 출구(검토 MED). 공제월이 끝난 뒤에만."""
    from erp.common import kst
    a = await _emp_with_july(session, "RS-10A")
    b = await _emp_with_july(session, "RS-10B")
    out = await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-10",
                           note="10월 정산")
    b.resigned_on = date(2026, 9, 30)                     # 실행 후 퇴사 — 10월 급여 없음
    await _pay.upsert_lines(session, actor=1, employee_id=a.id, yyyymm="2026-10",
                            lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    await _pay.confirm(session, actor=1, employee_id=a.id, yyyymm="2026-10")
    monkeypatch.setattr(kst, "kst_today", lambda: date(2026, 10, 31))
    with pytest.raises(ValidationError, match="그 달이 끝난 뒤"):
        await rs.absorb_remaining(session, actor=1, settlement_id=out["id"], reason="퇴사자 처리")
    monkeypatch.setattr(kst, "kst_today", lambda: date(2026, 11, 2))
    r = await rs.absorb_remaining(session, actor=1, settlement_id=out["id"], reason="9월 퇴사자 미공제분")
    assert r["employees"] == 1 and r["absorbed_now"] == 9_400
    assert await _voucher(session, r["voucher"]) == {("5330", "DEBIT"): 9_400, ("2110", "CREDIT"): 9_400}
    with pytest.raises(ValidationError, match="없습니다"):
        await rs.absorb_remaining(session, actor=1, settlement_id=out["id"], reason="두 번째")


async def test_cancel_does_not_re_reverse_manual_reversal(session, monkeypatch):
    """재무가 수동 역분개한 남은 공제분 전표 — 취소가 그 역분개 전표까지 뒤집어 금액을 되살리던 결함(재검토 MED)."""
    from erp.common import kst
    from erp.finance.service import VoucherService
    await _emp_with_july(session, "RS-11A")
    other = Employee(employee_no="RS-11X", name="x", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                     job_group="STAFF", source="ERP")
    session.add(other)
    await session.flush()
    session.add(PayrollRecord(employee_id=other.id, yyyymm="2026-10", confirmed_at=datetime.now(UTC)))
    await session.flush()
    out = await rs.execute(session, actor=1, source="2026-07", method="NEXT_PAYROLL_ADJUST", target="2026-10",
                           note="10월 정산")
    monkeypatch.setattr(kst, "kst_today", lambda: date(2026, 11, 2))
    r = await rs.absorb_remaining(session, actor=1, settlement_id=out["id"], reason="미생성 직원 처리")
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == r["voucher"]))
    await VoucherService().reverse(session, voucher_id=v.id, reason="재무 수동 역분개")
    await rs.cancel(session, actor=1, settlement_id=out["id"], reason="방식 재검토")
    live = (await session.scalars(select(Voucher).where(
        Voucher.source_ref.like(f"rate-settlement:{out['id']}:%"), Voucher.status != "REVERSED"))).all()
    net = 0
    for x in live:
        for ln in await session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == x.id,
                                                                  VoucherLine.account_code == "5330")):
            net += int(ln.amount) * (1 if ln.side == "DEBIT" else -1)
    assert net == -9_400 and len(live) == 1             # 원 전표(REVERSED) + 역분개 1건만 — 재역분개 없음


async def test_not_payroll_sot_records_only(session):
    e = await _emp_with_july(session, "RS-6")
    n_v = await session.scalar(select(func.count()).select_from(Voucher))
    out = await rs.execute(session, actor=1, source="2026-07", method="NOT_PAYROLL_SOT", target=None,
                           note="외부 급여 시스템이 원천")
    assert out["voucher_ref"] is None and await session.scalar(select(func.count()).select_from(Voucher)) == n_v
    await _pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-10",
                            lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    assert await _adj_lines(session, e.id, "2026-10") == {}


async def test_http_decide_is_blocked_and_execute_via_screen(session, client):
    from tests._http import auth_as
    await _emp_with_july(session, "RS-7")
    d = await _decision(session)
    h = await auth_as(session, user_id=961, role="ADMIN")
    r = await client.post(f"/api/v1/admin/decisions/{d.id}/decide", headers=h,
                          json={"option": "NEXT_PAYROLL_ADJUST"})
    assert r.status_code == 422 and "실행 화면" in r.text
    lst = (await client.get("/api/v1/admin/decisions", headers=h)).json()["items"]
    assert next(x for x in lst if x["key"] == _KEY)["execute_screen"] == "/admin/payroll-settings"
    r = await client.get("/api/v1/hr/statutory-params/rate-settlement/preview?yyyymm=2026-07"
                         "&method=NEXT_PAYROLL_ADJUST&target_yyyymm=2026-10", headers=h)
    assert r.status_code == 200 and r.json()["employee_total"] == 9_400, r.text
    r = await client.post("/api/v1/hr/statutory-params/rate-settlement", headers=h, json={
        "source_yyyymm": "2026-07", "method": "NEXT_PAYROLL_ADJUST", "target_yyyymm": "2026-10",
        "note": "화면 실행"})
    assert r.status_code == 201, r.text
    r = await client.post(f"/api/v1/admin/decisions/{d.id}/reopen", headers=h, json={"note": "다시"})
    assert r.status_code == 422 and "취소" in r.text
    ov = (await client.get("/api/v1/hr/statutory-params/rate-settlement?yyyymm=2026-07", headers=h)).json()
    assert ov["decision"]["status"] == "DECIDED" and ov["settlements"][0]["deduct"] == 1
    r = await client.post(f"/api/v1/hr/statutory-params/rate-settlement/{ov['settlements'][0]['id']}/cancel",
                          headers=h, json={"reason": "공제월 변경"})
    assert r.status_code == 200, r.text
