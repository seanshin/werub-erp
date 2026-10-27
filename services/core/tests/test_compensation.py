"""급여 기초정보(보수 프로필)·온보딩 보완입력 — upsert·암호화·누락식별·generate 폴백."""
from datetime import date

import pytest

from erp.errors import ValidationError
from erp.hr.compensation_service import CompensationService
from erp.hr.models import Employee
from erp.hr.router import _resolve_pay_basis

svc = CompensationService()


def _emp(no: str, name: str, **kw) -> Employee:
    defaults = dict(employee_no=no, name=name, hired_on=date(2025, 1, 1),
                    job_group="STAFF", employment_type="REGULAR", source="ERP")
    defaults.update(kw)
    return Employee(**defaults)


async def test_upsert_and_masked_account(session):
    emp = _emp("E0001", "김행정")
    session.add(emp)
    await session.flush()

    p = await svc.upsert(session, actor=1, employee_id=emp.id, base_pay=3_200_000,
                         dependents=2, bank_name="국민", bank_account="123456-04-987654")
    assert float(p.base_pay) == 3_200_000
    assert svc.masked_account(p) == "***7654"          # 평문 미노출 — 마스킹만

    # 갱신 — 동일 프로필(PK 유지), 값 교체
    p2 = await svc.upsert(session, actor=1, employee_id=emp.id, base_pay=3_500_000)
    assert p2.id == p.id and float(p2.base_pay) == 3_500_000


async def test_upsert_rejects_unknown_employee_and_negative(session):
    with pytest.raises(ValidationError):
        await svc.upsert(session, actor=1, employee_id=999, base_pay=1)
    emp = _emp("E0002", "이음수")
    session.add(emp)
    await session.flush()
    with pytest.raises(ValidationError):
        await svc.upsert(session, actor=1, employee_id=emp.id, base_pay=-100)


async def test_onboarding_pending_identifies_missing(session):
    regular = _emp("E0010", "박미등록")                                   # 프로필 없음
    contract = _emp("E0011", "최계약", employment_type="CONTRACT")        # 계약만료일 없음
    daily = _emp("E0012", "정일용", employment_type="DAILY")              # 일당 없음
    resigned = _emp("E0013", "퇴사자", resigned_on=date(2026, 1, 31))     # 제외 대상
    complete = _emp("E0014", "온전한")
    session.add_all([regular, contract, daily, resigned, complete])
    await session.flush()
    await svc.upsert(session, actor=1, employee_id=complete.id,
                     base_pay=3_000_000, bank_account="110-123-456789")

    items = {i["employee_no"]: i["missing"] for i in await svc.onboarding_pending(session)}
    assert "base_pay" in items["E0010"] and "compensation_profile" in items["E0010"]
    assert "contract_end" in items["E0011"]
    assert "daily_wage" in items["E0012"]
    assert "E0013" not in items                        # 퇴직자 제외
    assert "E0014" not in items                        # 완비 직원 제외


async def test_generate_falls_back_to_profile(session):
    emp = _emp("E0020", "한간호", job_group="NURSE", job_category="NURSE")
    session.add(emp)
    await session.flush()

    # 프로필 미등록 + 월별 입력 없음 → 온보딩 유도 오류
    with pytest.raises(ValidationError):
        await _resolve_pay_basis(session, emp, None, None)

    await svc.upsert(session, actor=1, employee_id=emp.id, base_pay=2_900_000, dependents=3)
    base, dep = await _resolve_pay_basis(session, emp, None, None)
    assert (base, dep) == (2_900_000, 3)               # 프로필 폴백

    base, dep = await _resolve_pay_basis(session, emp, 3_100_000, None)
    assert (base, dep) == (3_100_000, 3)               # 월별 입력 우선, 부양가족은 프로필

    # 일용직은 base_pay 0 허용(일당×일수 산정)
    daily = _emp("E0021", "주일용", employment_type="DAILY", daily_wage=150_000)
    session.add(daily)
    await session.flush()
    base, dep = await _resolve_pay_basis(session, daily, None, None)
    assert base == 0.0 and dep == 1


async def test_onboarding_grid_and_bulk_save(session):
    """스프레드시트 일괄 편집 — grid 조회(대기자+현재값) + bulk 저장(기본급>0만·savepoint 격리)."""
    from erp.common.router import CurrentUser
    from erp.hr.router import (BulkCompBody, BulkCompRow, onboarding_grid,
                               onboarding_grid_save)
    e1, e2 = _emp("E0G1", "직원A"), _emp("E0G2", "직원B")
    session.add_all([e1, e2])
    await session.flush()
    admin = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
    g = await onboarding_grid(user=admin, session=session)
    nos = {r["employee_no"] for r in g["items"]}
    assert "E0G1" in nos and "E0G2" in nos            # 둘 다 미등록 → 대기
    out = await onboarding_grid_save(
        body=BulkCompBody(rows=[
            BulkCompRow(employee_id=e1.id, base_pay=3_000_000, dependents=2, bank_name="국민",
                        bank_account="110-222-333444"),          # 계좌 입력 → 암호화 저장
            BulkCompRow(employee_id=e2.id, base_pay=0)]),        # 기본급 0 → 건너뜀
        user=admin, session=session)
    assert out["saved"] == 1 and out["skipped"] == 1
    p = await svc.get(session, employee_id=e1.id)
    assert float(p.base_pay) == 3_000_000 and p.dependents == 2
    assert svc.masked_account(p) == "***3444"                    # 계좌 암호화·마스킹
    # 재저장 시 계좌 미입력(None)이면 기존 계좌 보존
    await onboarding_grid_save(body=BulkCompBody(rows=[
        BulkCompRow(employee_id=e1.id, base_pay=3_100_000, dependents=2)]), user=admin, session=session)
    p2 = await svc.get(session, employee_id=e1.id)
    assert svc.masked_account(p2) == "***3444" and float(p2.base_pay) == 3_100_000  # 계좌 보존·기본급 갱신
