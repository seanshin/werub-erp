"""2026-10-10 — 인사 월 컬럼 저장형 "YYYY-MM" 단일화(common.months·0258).

종전: 라우터가 "202607"·"2026-07"을 원문 그대로 저장 → 같은 직원·같은 달 급여가 두 행(유니크=직원+원문)·
압축형은 [5:7] 슬라이스가 틀린 월을 읽었다.
"""
from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from erp.common.months import canonical_ym, require_ym
from erp.errors import ValidationError
from erp.hr.employee_service import PayrollService
from erp.hr.models import Employee, PayrollRecord


def _emp(no: str) -> Employee:
    return Employee(employee_no=no, name=f"직원{no}", hired_on=date(2024, 1, 1), job_group="STAFF",
                    employment_type="REGULAR", source="ERP")


@pytest.mark.parametrize("raw,want", [("2026-07", "2026-07"), ("202607", "2026-07"),
                                      (" 2026-12 ", "2026-12"), ("202601", "2026-01")])
def test_canonical_ym_accepts_both_forms(raw, want):
    assert canonical_ym(raw) == want


@pytest.mark.parametrize("raw", ["２０２６-07", "٢٠٢٦07", "2026-13", "202600", "2026-7", "20267", "2026/07", "2026--07",
                                 "2026-07-01", "abcd-07", ""])
def test_canonical_ym_rejects_malformed(raw):
    with pytest.raises(ValueError):
        canonical_ym(raw)
    with pytest.raises(ValidationError):
        require_ym(raw)


async def test_compact_and_dashed_put_hit_one_record(session, client):
    from tests._http import auth_as
    emp = _emp("YM-1")
    session.add(emp)
    await session.flush()
    h = await auth_as(session, user_id=951, role="ADMIN")
    body = {"employee_id": emp.id, "lines": [{"item_code": "BASE", "item_kind": "EARNING", "amount": 1}]}
    for ym in ("202607", "2026-07"):
        r = await client.put(f"/api/v1/hr/payrolls/{ym}", headers=h, json=body)
        assert r.status_code == 200, r.text
    rows = (await session.scalars(select(PayrollRecord.yyyymm).where(
        PayrollRecord.employee_id == emp.id))).all()
    assert rows == ["2026-07"]                       # 종전: ["202607", "2026-07"] 두 행


async def test_malformed_month_path_is_422(session, client):
    from tests._http import auth_as
    h = await auth_as(session, user_id=952, role="ADMIN")
    r = await client.get("/api/v1/hr/statutory-params/rate-diff?yyyymm=2026-13", headers=h)
    assert r.status_code == 422, r.text
    r = await client.put("/api/v1/hr/payrolls/2026-13", headers=h, json={"employee_id": 1, "lines": []})
    assert r.status_code == 422, r.text


async def test_service_normalizes_compact_month(session):
    emp = _emp("YM-2")
    session.add(emp)
    await session.flush()
    rec = await PayrollService().upsert_lines(session, actor=1, employee_id=emp.id, yyyymm="202608",
                                              lines=[{"item_code": "BASE", "item_kind": "EARNING",
                                                      "amount": 1}])
    assert rec.yyyymm == "2026-08"


async def test_db_check_rejects_compact_month(session):
    emp = _emp("YM-3")
    session.add(emp)
    await session.flush()
    n = await session.scalar(select(func.count()).select_from(PayrollRecord))
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(PayrollRecord(employee_id=emp.id, yyyymm="202607"))
            await session.flush()
    assert await session.scalar(select(func.count()).select_from(PayrollRecord)) == n


@pytest.mark.parametrize("bad", ["abcd-07", "2026-0:", "2026-1a", "20x6-07", "２０２６-07"])
async def test_db_check_rejects_non_digit_months(session, bad):
    emp = _emp(f"YM-D{bad}")
    session.add(emp)
    await session.flush()
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(PayrollRecord(employee_id=emp.id, yyyymm=bad))
            await session.flush()


async def test_cost_tables_check_and_router_422(session, client):
    from erp.finance.models import CostDriverValue
    from tests._http import auth_as
    with pytest.raises(IntegrityError):
        async with session.begin_nested():
            session.add(CostDriverValue(period="202605", department_id=1, driver="CASES", value=1))
            await session.flush()
    h = await auth_as(session, user_id=953, role="ADMIN")
    r = await client.get("/api/v1/costing/summary?yyyymm=2026-13", headers=h)
    assert r.status_code == 422, r.text
