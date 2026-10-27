"""v1.303 — 급여 법정 점검: 최저임금(산입범위·단시간·일할·일용)·비과세 한도의 4대보험 보수월액 반영."""
from datetime import UTC, date, datetime

from erp.hr import payroll_checks as pc
from erp.hr import payroll_rules, statutory
from erp.hr.models import Employee, PayrollLine, PayrollRecord


def _l(code, amt, kind="EARNING"):
    return {"item_code": code, "item_kind": kind, "amount": amt}


def _chk(lines, **kw):
    base = {"hourly_min": 10_320, "employment_type": "REGULAR", "weekly_hours": None, "employed_days": 31, "month_days": 31}
    base.update(kw)
    return pc.check_min_wage(lines, **base)


def test_inclusion_scope_and_threshold():
    """산입=기본급·정기상여·고정수당·식대 / 제외=연장·야간·휴일·당직·성과급. 2026 기준 2,156,880원(209h)."""
    lines = [_l("BASE", 2_000_000), _l("OT", 500_000), _l("NIGHT", 240_000), _l("DUTY", 200_000), _l("INCENTIVE", 1_000_000)]
    r = _chk(lines)
    assert not r["ok"] and r["threshold"] == 2_156_880 and r["included"] == 2_000_000 and r["shortfall"] == 156_880
    assert _chk(lines + [_l("MEAL", 200_000)])["ok"]                   # 식대(현금성 복리후생) 산입
    r = _chk([_l("BASE", 1_100_000)], weekly_hours=20, employed_days=30, month_days=30)
    assert r["ok"] and r["threshold"] == 1_078_440                     # 단시간(주 20h) — 기준 절반
    r = _chk([_l("BASE", 1_000_000)], employed_days=15, month_days=30)
    assert not r["ok"] and r["threshold"] == 1_078_440                 # 입사월 일할
    assert _chk([_l("BASE", 1)], hourly_min=None) == {"skip": "최저임금 미등록"}


def test_unpaid_leave_reduces_hours_too():
    """무급휴가 — 임금만 빼고 시간을 안 빼면 적법한 급여가 미달로 나왔다(검토 MED: 230만·무급 3일)."""
    from erp.hr import payroll_rules
    r = payroll_rules.build_payroll(base_pay=2_300_000, dependents=1, year=2026, month=10, unpaid_days=3)
    res = _chk(r["lines"])
    assert res["ok"] and res["unpaid_days"] == 3 and res["hours"] == 209 - 24


def test_watch_requires_registered_hours_and_unclassified_counted():
    """감시단속은 209h 가정 불가(격일 24h 등) — 주 소정시간 없으면 보류·있으면 상한 없이. 미분류 고정 수당은 산입·표시."""
    assert "감시단속" in _chk([_l("BASE", 2_000_000)], employment_type="WATCH")["skip"]
    r = _chk([_l("BASE", 2_400_000)], employment_type="WATCH", weekly_hours=60)
    assert not r["ok"] and r["hours"] == 313.5                         # 209 × 60/40
    r = _chk([_l("BASE", 2_000_000), _l("POSITION", 200_000)])
    assert r["ok"] and r["unclassified"] == ["POSITION"]


def test_daily_uses_recorded_daily_wage():
    lines = [{"item_code": "BASE", "item_kind": "EARNING", "amount": 240_000, "calc_basis": {"daily": 80_000, "days": 3}}]
    r = _chk(lines, employment_type="DAILY")
    assert not r["ok"] and r["threshold"] == 82_560
    assert _chk([_l("BASE", 1)], employment_type="DAILY") == {"skip": "일당 기록 없음"}


def test_min_wage_values_and_db_override():
    assert statutory.min_wage_hourly(2026) == (10_320, "코드 고시값") and statutory.min_wage_hourly(2027)[0] == 10_700
    assert statutory.min_wage_hourly(2031) == (None, "미등록")
    statutory._DB_INSURANCE[2031] = {"min_wage_hourly": 12_000}
    try:
        assert statutory.min_wage_hourly(2031) == (12_000, "법정 요율 등록")
    finally:
        statutory._DB_INSURANCE.pop(2031, None)


def test_insurance_base_excludes_nontax_meal():
    """보수월액 = 비과세 제외 — 식대 한도(20만)까지는 4대보험·소득세 기준에서 빠지고 초과분만 들어간다."""
    base = payroll_rules.build_payroll(base_pay=3_000_000, dependents=1, year=2026, month=10)
    def np(r):
        return next(x for x in r["lines"] if x["item_code"] == "NP")["calc_basis"]["base"]
    assert np(base) == 3_000_000
    # build_payroll은 식대 줄을 만들지 않는다 — 비과세 사전으로 BASE 일부를 비과세로 가정해 기준 차감을 확인
    r = payroll_rules.build_payroll(base_pay=3_000_000, dependents=1, year=2026, month=10, nontax={"BASE": 200_000})
    assert np(r) == 2_800_000


async def test_month_report_and_action_center(session, client):
    from erp.app import action_center as ac
    from tests._http import auth_as
    lo = Employee(employee_no="MW-1", name="미달", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                  job_group="STAFF", source="ERP")
    ok = Employee(employee_no="MW-2", name="충족", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                  job_group="STAFF", source="ERP")
    session.add_all([lo, ok])
    await session.flush()
    for e, amt, conf in ((lo, 2_000_000, False), (ok, 2_500_000, False)):
        r = PayrollRecord(employee_id=e.id, yyyymm="2026-10", confirmed_at=datetime.now(UTC) if conf else None)
        session.add(r)
        await session.flush()
        session.add_all([PayrollLine(record_id=r.id, item_code="BASE", item_kind="EARNING", amount=amt),
                         PayrollLine(record_id=r.id, item_code="OT", item_kind="EARNING", amount=900_000)])
    await session.flush()
    h = await auth_as(session, user_id=1, role="ADMIN")
    rep = (await client.get("/api/v1/hr/payrolls/2026-10/min-wage", headers=h)).json()
    assert rep["checked"] == 2 and [v["employee_no"] for v in rep["violations"]] == ["MW-1"]
    assert rep["violations"][0]["shortfall"] == 156_880
    item = next(a for a in (await ac.screen_actions(session, today=date(2026, 10, 11)))["actions"]
                if a["key"] == "min-wage-2026-10")
    assert item["count"] == 1 and "10,320" in item["detail"]
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/hr/payrolls/2026-10/min-wage", headers=staff)).status_code == 403


async def test_insurance_endpoint_and_employer_base_exclude_nontax(session, client):
    """보수월액 단일 기준 — 4대보험 화면(insurance API)도 비과세 제외(종전: 지급 총액)."""
    from erp.hr.models import PayItemCatalog
    from tests._http import auth_as
    if await session.get(PayItemCatalog, "MEAL") is None:
        session.add(PayItemCatalog(code="MEAL", name="식대", kind="EARNING", taxable=False, nontax_limit=200_000))
    e = Employee(employee_no="NT-1", name="식대", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    r = PayrollRecord(employee_id=e.id, yyyymm="2026-10")
    session.add(r)
    await session.flush()
    session.add_all([PayrollLine(record_id=r.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
                     PayrollLine(record_id=r.id, item_code="MEAL", item_kind="EARNING", amount=250_000)])
    await session.flush()
    h = await auth_as(session, user_id=1, role="ADMIN")
    got = (await client.get(f"/api/v1/hr/payrolls/2026-10/insurance?employee_id={e.id}", headers=h)).json()
    assert got["보수월액"] == 3_050_000                                # 식대 25만 중 한도 20만 비과세


def test_nontax_limit_applies_per_item_not_per_line():
    from erp.hr.payroll_rules import nontax_exempt
    lines = [_l("MEAL", 150_000), _l("MEAL", 150_000), _l("BASE", 3_000_000)]
    assert nontax_exempt(lines, {"MEAL": 200_000}) == 200_000
