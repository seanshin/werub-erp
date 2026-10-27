"""v1.304 — 연말정산 준비: 연간 집계(ESS 원천징수영수증 기준)·간이 정산(보험료 공제 포함)·확인할 점·엑셀."""
from datetime import UTC, date, datetime

from erp.hr import statutory
from erp.hr.models import Dependent, Employee, PayItemCatalog, PayrollLine, PayrollRecord

NOW = datetime(2026, 10, 1, tzinfo=UTC)


async def _emp(session, no, **kw):
    e = Employee(employee_no=no, name=no, hired_on=date(2020, 1, 1), employment_type="REGULAR", job_group="STAFF",
                 source="ERP", **kw)
    session.add(e)
    await session.flush()
    return e


async def _rec(session, emp, ym, lines, confirmed=True):
    r = PayrollRecord(employee_id=emp.id, yyyymm=ym, confirmed_at=NOW if confirmed else None)
    session.add(r)
    await session.flush()
    for code, kind, amt, basis in lines:
        session.add(PayrollLine(record_id=r.id, item_code=code, item_kind=kind, amount=amt, calc_basis=basis))
    await session.flush()
    return r


async def test_annual_totals_rules_and_flags(session, client):
    from erp.hr import year_end
    from tests._http import auth_as
    if await session.get(PayItemCatalog, "MEAL") is None:
        session.add(PayItemCatalog(code="MEAL", name="식대", kind="EARNING", taxable=False, nontax_limit=200_000))
    a = await _emp(session, "YE-1")
    session.add(Dependent(employee_id=a.id, name="자녀", relation="CHILD", birth_date=date(2015, 5, 1), counted=True))
    approx = {"method": "근사산식", "dependents": 1}
    table = {"method": "간이세액표", "table_id": 1}
    for ym, basis in (("2026-01", approx), ("2026-02", table)):
        await _rec(session, a, ym, [("BASE", "EARNING", 3_000_000, None), ("MEAL", "EARNING", 250_000, None),
                                    ("TAX", "DEDUCTION", 100_000, basis), ("LOCALTAX", "DEDUCTION", 10_000, None),
                                    ("NP", "DEDUCTION", 140_000, None), ("HI", "DEDUCTION", 110_000, None)])
    await _rec(session, a, "2026-03", [("BASE", "EARNING", 3_000_000, None), ("INCENTIVE", "EARNING", 1_000_000, None),
                                       ("INCTAX", "DEDUCTION", 50_000, None)], confirmed=False)
    b = await _emp(session, "YE-2")
    await _rec(session, b, "2026-01", [("BASE", "EARNING", 2_500_000, None), ("TAX", "DEDUCTION", 30_000, table)])
    t = (await year_end.annual_totals(session, year=2026))
    ta = t[a.id]
    # 확정 2개월 + 미확정 달의 성과급 외부 줄 · 식대는 월 한도 20만씩 비과세
    assert ta["gross"] == 2 * 3_250_000 + 1_000_000 and ta["nontax"] == 400_000 and ta["taxable"] == 7_100_000
    assert ta["income_tax"] == 250_000 and ta["pension"] == 280_000 and ta["insurance"] == 220_000
    assert ta["approx_months"] == ["2026-01"] and ta["unconfirmed_months"] == ["2026-03"]
    h = await auth_as(session, user_id=1, role="ADMIN")
    r = (await client.get("/api/v1/hr/year-end/2026/readiness", headers=h)).json()
    row = next(x for x in r["rows"] if x["employee_no"] == "YE-1")
    assert row["family"] == 2 and row["children"] == 1 and "근사 산식 원천징수 1개월" in row["flags"]
    assert "미확정 1개월" in row["flags"]
    assert "부양가족 미등록(프로필 가족 수)" in next(x for x in r["rows"] if x["employee_no"] == "YE-2")["flags"]
    assert r["summary"]["people"] == 2 and r["summary"]["approx_people"] == 1
    # 직원 간이 정산 화면과 같은 값(단일 소스)
    one = (await client.get(f"/api/v1/hr/employees/{a.id}/year-end?year=2026", headers=h)).json()
    assert one["결정세액"] == row["determined"] and one["정산액"] == row["settlement"] and one["총급여"] == 7_100_000
    assert one["dependents_source"] == "부양가족 등록" and one["children"] == 1
    x = await client.get("/api/v1/hr/year-end/2026/readiness.xlsx", headers=h)
    assert x.status_code == 200 and x.content[:2] == b"PK"
    mgr = await auth_as(session, user_id=2, role="MANAGER")
    assert (await client.get("/api/v1/hr/year-end/2026/readiness", headers=mgr)).status_code == 403


def test_settlement_deducts_paid_insurance():
    """연금보험료·건강·고용보험료 공제 — 같은 총급여에서 결정세액이 줄어든다(종전: 미공제로 결정세액 과대)."""
    a = statutory.year_end_settlement(40_000_000, withheld_tax=1_000_000, year=2026, dependents=1)
    b = statutory.year_end_settlement(40_000_000, withheld_tax=1_000_000, year=2026, dependents=1,
                                      pension_paid=1_900_000, insurance_paid=1_700_000)
    assert b["과세표준"] == a["과세표준"] - 3_600_000 and b["결정세액"] < a["결정세액"]
    assert b["연금보험료공제"] == 1_900_000 and b["보험료공제"] == 1_700_000


async def test_daily_excluded_and_ess_matches_readiness(session, client):
    """일용(분리과세)은 연말정산 집계에서 빠지고 · ESS 원천징수영수증 정산 = 준비 화면 정산(식대가 있어도 같은 총급여)."""
    from erp.config import get_settings
    from erp.hr import year_end
    from erp.integrations.ess_router import withholding_receipt
    from tests._http import auth_as
    if await session.get(PayItemCatalog, "MEAL") is None:
        session.add(PayItemCatalog(code="MEAL", name="식대", kind="EARNING", taxable=False, nontax_limit=200_000))
    d = await _emp(session, "YE-D")
    d.employment_type = "DAILY"
    await _rec(session, d, "2026-01", [("BASE", "EARNING", 1_500_000, {"daily": 150_000, "days": 10}),
                                       ("TAX", "DEDUCTION", 2_700, {"method": "일용분리과세"})])
    m = await _emp(session, "YE-M")
    for ym in ("2026-01", "2026-02", "2026-03"):
        await _rec(session, m, ym, [("BASE", "EARNING", 3_000_000, None), ("MEAL", "EARNING", 200_000, None),
                                    ("TAX", "DEDUCTION", 80_000, {"method": "간이세액표", "table_id": 1}),
                                    ("NP", "DEDUCTION", 135_000, None), ("HI", "DEDUCTION", 106_000, None)])
    t = await year_end.annual_totals(session, year=2026)
    assert t[d.id]["taxable"] == 0 and t[d.id]["daily_months"] == ["2026-01"]
    h = await auth_as(session, user_id=1, role="ADMIN")
    rows = {x["employee_no"]: x for x in (await client.get("/api/v1/hr/year-end/2026/readiness", headers=h)).json()["rows"]}
    assert rows["YE-D"]["determined"] is None and "일용(분리과세) 1개월 제외" in rows["YE-D"]["flags"]
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "ik"
    try:
        out = await withholding_receipt(employeeNumber="YE-M", year=2026, x_integration_key="ik", session=session)
    finally:
        s.his_webhook_key = old
    assert out["totalGross"] == 9_600_000 and out["totalTaxable"] == 9_000_000 and out["nontax"] == 600_000
    assert out["settlement"]["determinedTax"] == rows["YE-M"]["determined"]
    assert out["settlement"]["balance"] == rows["YE-M"]["settlement"]


def test_standard_credit_when_better():
    """저소득·보험료 적은 경우 표준세액공제(13만)가 유리 — 둘 중 결정세액이 작은 쪽."""
    lo = statutory.year_end_settlement(20_000_000, withheld_tax=200_000, year=2026, dependents=1,
                                       pension_paid=900_000, insurance_paid=300_000)
    assert lo["공제 방식"] == "표준세액공제(13만)" and lo["보험료공제"] == 0
    hi = statutory.year_end_settlement(80_000_000, withheld_tax=5_000_000, year=2026, dependents=1,
                                       pension_paid=3_000_000, insurance_paid=3_000_000)
    assert hi["공제 방식"] == "보험료 특별소득공제"


async def test_employee_children_capped(session, client):
    from tests._http import auth_as
    e = await _emp(session, "YE-C")
    await _rec(session, e, "2026-01", [("BASE", "EARNING", 3_000_000, None)])
    h = await auth_as(session, user_id=1, role="ADMIN")
    r = (await client.get(f"/api/v1/hr/employees/{e.id}/year-end?year=2026&children=3", headers=h)).json()
    assert r["children"] == 0                          # 가족 1명(본인)이면 자녀 공제 인원 0
    assert (await client.get("/api/v1/hr/year-end/1999/readiness.xlsx", headers=h)).status_code == 422
