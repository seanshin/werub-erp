"""2026-10-09 전체 흐름 검토 C — 급여·퇴직·세무 집계 결함 회귀.

각 테스트는 수정 전 코드에서 실패함을 확인했다(되돌려 재현).
"""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Voucher, VoucherLine
from erp.hr import payroll_rules, statutory
from erp.hr.compensation_service import CompensationService
from erp.hr.employee_service import PayrollService
from erp.hr.models import Employee, PayrollLine, PayrollRecord

_pay = PayrollService()


def _emp(no: str, **kw) -> Employee:
    d = dict(employee_no=no, name=f"직원{no}", hired_on=date(2024, 1, 1), job_group="STAFF",
             employment_type="REGULAR", source="ERP")
    d.update(kw)
    return Employee(**d)


async def _voucher_lines(session, ref: str) -> dict:
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ref))
    assert v is not None
    out: dict = {}
    for ln in (await session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == v.id))):
        out[(ln.account_code, ln.side)] = out.get((ln.account_code, ln.side), 0) + float(ln.amount)
    return out


# ── #1 급여 확정 전표가 성과급 원천징수(INCTAX)를 2150에 또 넣던 이중계상 ──────────────
async def test_confirm_excludes_external_inctax_from_withholding(session):
    emp = _emp("FF-1")
    session.add(emp)
    await session.flush()
    await _pay.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06", lines=[
        {"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000},
        {"item_code": "TAX", "item_kind": "DEDUCTION", "amount": 100_000}])
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == emp.id))
    # 성과급 승인 워크플로가 붙인 외부 라인(전표는 성과급 전표가 별도로 2150 전기)
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000),
        PayrollLine(record_id=rec.id, item_code="INCTAX", item_kind="DEDUCTION", amount=50_000)])
    await session.flush()
    await _pay.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    by = await _voucher_lines(session, f"payroll:{rec.id}")
    assert by[("5310", "DEBIT")] == 3_000_000
    assert by[("2150", "CREDIT")] == 100_000          # 종전 150,000(INCTAX 이중)
    assert by[("2110", "CREDIT")] == 2_900_000        # 종전 2,850,000(순지급 과소)


# ── #6 확정 급여 잠금 ──────────────────────────────────────────────────────
async def test_confirmed_payroll_cannot_be_regenerated(session):
    emp = _emp("FF-6")
    session.add(emp)
    await session.flush()
    await _pay.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06", lines=[
        {"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    await _pay.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    with pytest.raises(ValidationError, match="이미 확정"):
        await _pay.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06", lines=[
            {"item_code": "BASE", "item_kind": "EARNING", "amount": 9_000_000}])
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == emp.id))
    base = await session.scalar(select(PayrollLine.amount).where(
        PayrollLine.record_id == rec.id, PayrollLine.item_code == "BASE"))
    assert float(base) == 3_000_000                    # 명세가 전표(최초 확정분)와 정합 유지


async def test_put_on_confirmed_payroll_returns_error(session, client):
    from tests._http import auth_as
    emp = _emp("FF-6H")
    session.add(emp)
    await session.flush()
    await _pay.upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="2026-06", lines=[
        {"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    await _pay.confirm(session, actor=1, employee_id=emp.id, yyyymm="2026-06")
    h = await auth_as(session, user_id=901, role="ADMIN")
    r = await client.put("/api/v1/hr/payrolls/2026-06", headers=h, json={
        "employee_id": emp.id,
        "lines": [{"item_code": "BASE", "item_kind": "EARNING", "amount": 1}]})
    assert r.status_code in (400, 422), r.text
    assert "확정" in r.text


# ── #2 일괄 생성 대상 선정·입퇴사월 일할 ───────────────────────────────────────
async def test_bulk_generate_targets_and_prorates(session, client):
    from tests._http import auth_as
    cs = CompensationService()
    full = _emp("FF-2A")                                              # 정상 재직
    future_resign = _emp("FF-2B", resigned_on=date(2026, 12, 31))     # 퇴사 예정(재직 중)
    mid_hire = _emp("FF-2C", hired_on=date(2026, 7, 16))              # 7/16 입사 → 16/31
    mid_resign = _emp("FF-2D", resigned_on=date(2026, 7, 10))         # 7/10 퇴사 → 10/31
    late_hire = _emp("FF-2E", hired_on=date(2026, 8, 3))              # 월말 이후 입사 → 제외
    gone = _emp("FF-2F", resigned_on=date(2026, 6, 30))               # 전월 퇴사 → 제외
    session.add_all([full, future_resign, mid_hire, mid_resign, late_hire, gone])
    await session.flush()
    for e in (full, future_resign, mid_hire, mid_resign, late_hire, gone):
        await cs.upsert(session, actor=1, employee_id=e.id, base_pay=3_100_000)
    h = await auth_as(session, user_id=902, role="ADMIN")
    r = await client.post("/api/v1/hr/payrolls/2026-07/generate-bulk", headers=h)
    assert r.status_code == 200, r.text
    recs = {x["employee_id"]: x for x in r.json()["records"]}
    assert set(recs) == {full.id, future_resign.id, mid_hire.id, mid_resign.id}
    assert "prorated" not in recs[full.id] and "prorated" not in recs[future_resign.id]
    assert recs[mid_hire.id]["prorated"]["employed_days"] == 16
    assert recs[mid_resign.id]["prorated"]["employed_days"] == 10

    async def base_of(emp_id: int) -> float:
        rec = await session.scalar(select(PayrollRecord).where(
            PayrollRecord.employee_id == emp_id, PayrollRecord.yyyymm == "2026-07"))
        return float(await session.scalar(select(PayrollLine.amount).where(
            PayrollLine.record_id == rec.id, PayrollLine.item_code == "BASE")))
    assert await base_of(full.id) == 3_100_000
    assert await base_of(mid_hire.id) == 1_600_000      # 3,100,000 × 16/31
    assert await base_of(mid_resign.id) == 1_000_000    # 3,100,000 × 10/31


def test_proration_keeps_hourly_rate_on_full_monthly_base():
    """일할은 BASE에만 — 통상시급(연장 단가)은 월 전액 기본급 기준(시급 = 월급 ÷ 209h)."""
    out = payroll_rules.build_payroll(base_pay=2_090_000, ot_hours=10, employed_days=15,
                                      month_days=30)
    lines = {ln["item_code"]: ln for ln in out["lines"]}
    assert lines["BASE"]["amount"] == 1_045_000
    assert lines["OT"]["amount"] == 150_000              # 10h × 10,000 × 1.5


# ── #5 무급휴가 공제 상한 ─────────────────────────────────────────────────
def test_unpaid_leave_deduction_capped_at_base_pay():
    out = payroll_rules.build_payroll(base_pay=2_090_000, unpaid_days=31)   # 80,000 × 31 = 2.48M
    lines = {ln["item_code"]: ln for ln in out["lines"]}
    assert lines["UNPAID_LEAVE"]["amount"] == -2_090_000
    assert lines["UNPAID_LEAVE"]["calc_basis"]["capped"] is True
    assert out["gross"] == 0                              # 종전 −390,000(음수 급여)


# ── #4 일괄 경로 토요일 연장 분리 ────────────────────────────────────────────
async def test_bulk_inputs_apply_saturday_ot_split_like_individual(session):
    from erp.hr.models import AttendanceSummary, GroupwareAttendanceStaging
    from erp.hr.pay_inputs import resolve_monthly_inputs, resolve_monthly_inputs_bulk
    office = _emp("FF-4")
    session.add(office)
    await session.flush()
    session.add(GroupwareAttendanceStaging(
        source_ref="his:FF-4:2026-07-04", external_user_id="x", employee_no="FF-4",
        work_on=date(2026, 7, 4), clock_in=datetime(2026, 7, 4, 9, 0),
        clock_out=datetime(2026, 7, 4, 17, 0), status="APPLIED"))
    session.add(AttendanceSummary(employee_id=office.id, yyyymm="2026-07",
                                  work_days=23, total_work_minutes=184 * 60))
    await session.flush()
    single = await resolve_monthly_inputs(session, office, "2026-07")
    bulk = (await resolve_monthly_inputs_bulk(session, [office], "2026-07"))[office.id]
    assert single["ot_hours"] == 8.0
    assert bulk["ot_hours"] == single["ot_hours"]         # 종전 일괄=0(토요일 소멸)


# ── #7 연말정산 기납부세액에 성과급 원천징수 포함 ────────────────────────────────
async def test_year_end_withheld_includes_inctax(session):
    from erp.common.router import CurrentUser
    from erp.hr.router import employee_year_end
    emp = _emp("FF-7")
    session.add(emp)
    await session.flush()
    rec = PayrollRecord(employee_id=emp.id, yyyymm="2026-03",
                        confirmed_at=datetime(2026, 3, 25, tzinfo=timezone.utc))
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=80_000),
        PayrollLine(record_id=rec.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000),
        PayrollLine(record_id=rec.id, item_code="INCTAX", item_kind="DEDUCTION", amount=40_000)])
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    out = await employee_year_end(employee_id=emp.id, year=2026, user=admin, session=session)
    assert out["기납부세액"] == 120_000                   # 종전 80,000


# ── #8 세무 집계 ───────────────────────────────────────────────────────────
async def test_withholding_confirmed_only_daily_split_local_separate(session):
    from erp.tax.service import TaxService
    reg, daily, draft = _emp("FF-8A"), _emp("FF-8B", employment_type="DAILY"), _emp("FF-8C")
    session.add_all([reg, daily, draft])
    await session.flush()
    conf = datetime(2026, 5, 25, tzinfo=timezone.utc)
    r1 = PayrollRecord(employee_id=reg.id, yyyymm="2026-05", confirmed_at=conf)
    r2 = PayrollRecord(employee_id=daily.id, yyyymm="2026-05", confirmed_at=conf)
    r3 = PayrollRecord(employee_id=draft.id, yyyymm="2026-05")          # 미확정 초안
    session.add_all([r1, r2, r3])
    await session.flush()
    session.add_all([
        PayrollLine(record_id=r1.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=r1.id, item_code="TAX", item_kind="DEDUCTION", amount=100_000),
        PayrollLine(record_id=r1.id, item_code="LOCALTAX", item_kind="DEDUCTION", amount=10_000),
        PayrollLine(record_id=r1.id, item_code="INCTAX", item_kind="DEDUCTION", amount=30_000),
        PayrollLine(record_id=r1.id, item_code="INCLOCALTAX", item_kind="DEDUCTION", amount=3_000),
        PayrollLine(record_id=r2.id, item_code="BASE", item_kind="EARNING", amount=2_000_000),
        PayrollLine(record_id=r2.id, item_code="TAX", item_kind="DEDUCTION", amount=13_500),
        PayrollLine(record_id=r3.id, item_code="BASE", item_kind="EARNING", amount=9_000_000),
        PayrollLine(record_id=r3.id, item_code="TAX", item_kind="DEDUCTION", amount=900_000)])
    await session.flush()
    w = await TaxService().withholding(session, yyyymm="2026-05")
    assert w["소득구분"] == "근로소득(A01)"
    assert (w["인원"], w["총지급액"], w["원천징수세액"]) == (1, 3_000_000, 130_000)
    assert w["지방소득세"] == 13_000                       # 소득세와 분리
    a03 = w["구분"][1]
    assert a03["소득구분"] == "일용근로소득(A03)" and a03["원천징수세액"] == 13_500
    assert w["합계"]["인원"] == 2                          # 미확정 초안 제외


async def test_insurance_filing_daily_work_days_and_excludes_incentive(session):
    from erp.tax.service import TaxService
    reg, daily = _emp("FF-8D"), _emp("FF-8E", employment_type="DAILY")
    session.add_all([reg, daily])
    await session.flush()
    conf = datetime(2026, 5, 25, tzinfo=timezone.utc)
    r1 = PayrollRecord(employee_id=reg.id, yyyymm="2026-05", confirmed_at=conf)
    r2 = PayrollRecord(employee_id=daily.id, yyyymm="2026-05", confirmed_at=conf)
    session.add_all([r1, r2])
    await session.flush()
    session.add_all([
        PayrollLine(record_id=r1.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=r1.id, item_code="INCENTIVE", item_kind="EARNING", amount=2_000_000),
        PayrollLine(record_id=r2.id, item_code="BASE", item_kind="EARNING", amount=2_000_000,
                    calc_basis={"days": 10, "daily": 200_000})])
    await session.flush()
    f = await TaxService().insurance_filing(session, yyyymm="2026-05")
    exp_reg = statutory.insurance(3_000_000, year=2026)
    exp_daily = statutory.insurance(2_000_000, year=2026, employment_type="DAILY", work_days=10)
    assert exp_daily["국민연금"] > 0                       # 월 8일 이상 → 연금 적용
    assert f["근로자부담"]["국민연금"] == exp_reg["국민연금"] + exp_daily["국민연금"]
    assert f["근로자부담"]["건강보험"] == exp_reg["건강보험"] + exp_daily["건강보험"]


async def test_combined_incentive_splits_local_tax_line(session):
    from erp.hr.incentive_service import IncentiveService
    from erp.hr.payroll_settings import PayrollSettingsService
    await PayrollSettingsService().upsert(session, actor=1,
                                          incentive_withholding_method="COMBINED", combined_months=1)
    emp = _emp("FF-8F")
    session.add(emp)
    await session.flush()
    await IncentiveService()._add_payroll_line(session, emp.id, "2026-06", 1_000_000,
                                               withheld=50_000, wh_formula="x",
                                               local_withheld=5_000)
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == emp.id))
    codes = {ln.item_code: float(ln.amount) for ln in (await session.scalars(
        select(PayrollLine).where(PayrollLine.record_id == rec.id)))}
    assert codes == {"INCENTIVE": 1_000_000, "INCTAX": 50_000, "INCLOCALTAX": 5_000}


# ── #3 퇴직소득세 원천징수 ─────────────────────────────────────────────────
def test_retirement_income_tax_reference_case():
    """퇴직금 1억·근속 10년 — 근속공제 1,500만 → 환산급여 1억200만 → 환산공제 6,260만 →
    과표 3,940만 → 환산산출 465만 → 산출 387.5만(국세청 퇴직소득 계산 절차)."""
    t = statutory.retirement_income_tax(100_000_000, years=10, year=2026)
    assert t["근속연수공제"] == 15_000_000
    assert t["환산급여"] == 102_000_000
    assert t["환산급여공제"] == 62_600_000
    assert t["과세표준"] == 39_400_000
    assert t["소득세"] == 3_875_000 and t["지방소득세"] == 387_500


def test_service_years_rounds_up_partial_year():
    assert statutory.service_years(date(2016, 1, 1), date(2025, 12, 31)) == 10   # 정확히 10년
    assert statutory.service_years(date(2016, 1, 1), date(2026, 1, 1)) == 11     # 하루 초과 → 올림
    assert statutory.service_years(date(2026, 1, 1), date(2026, 3, 1)) == 1


async def test_offboarding_settlement_withholds_retirement_tax(session):
    from erp.hr.absence_service import AbsenceService
    from erp.hr.offboarding_service import OffboardingService
    emp = _emp("FF-3", hired_on=date(2016, 1, 1))
    session.add(emp)
    await session.flush()
    await CompensationService().upsert(session, actor=1, employee_id=emp.id, base_pay=5_000_000)
    await AbsenceService().resign(session, actor=1, employee_id=emp.id,
                                  resigned_on=date(2026, 6, 30))
    out = await OffboardingService().settle(session, actor=1, employee_id=emp.id)
    wh = out["withholding"]
    assert wh["retirement_income_tax"] > 0 and wh["retirement_local_tax"] > 0
    by = await _voucher_lines(session, f"offboard:{emp.id}")
    total = float(out["severance"]["퇴직금"]) + out["leave_allowance"]
    assert by[("2150", "CREDIT")] == wh["total"]          # 종전 2150 없음(무징수)
    assert by[("2110", "CREDIT")] == total - wh["total"]
    debit = sum(v for (_a, side), v in by.items() if side == "DEBIT")
    assert debit == total


# ── 추가(리드 요청) 의료진 계약 결재선 우회 ──────────────────────────────────
async def test_medical_contract_approve_without_submit_builds_chain(session):
    from erp.common.models import ApprovalRule
    from erp.hr.contract_service import MedicalContractService
    from tests._http import make_user
    session.add(ApprovalRule(doc_type="MEDICAL_CONTRACT", min_amount=0, max_amount=10 ** 12,
                             active=True, steps=[{"type": "ROLE", "value": "FINANCE_MANAGER"},
                                                 {"type": "ROLE", "value": "ADMIN"}]))
    await make_user(session, user_id=31, role="STAFF")
    await make_user(session, user_id=32, role="FINANCE_MANAGER")
    await make_user(session, user_id=33, role="ADMIN")
    doc = _emp("FF-MC", staff_class="MEDICAL", job_category="DOCTOR", job_group="DOCTOR")
    session.add(doc)
    await session.flush()
    svc = MedicalContractService()
    c = await svc.create(session, actor=31, employee_id=doc.id, valid_from=date(2026, 7, 1),
                         base_pay=10_000_000)
    assert c.status == "DRAFT"
    c = await svc.approve(session, actor=32, contract_id=c.id)   # 상신 없이 바로 승인 시도
    assert c.status == "PENDING_APPROVAL"                         # 종전: 즉시 APPROVED(단일 승인)
    c = await svc.approve(session, actor=33, contract_id=c.id)
    assert c.status == "APPROVED"
