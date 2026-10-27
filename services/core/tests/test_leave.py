"""연차 산정 (FR-31, 근로기준법 §60) + 휴가 워크플로우 (FR-05)."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ForbiddenError, ValidationError
from erp.hr.leave_service import LeaveService, annual_leave_days
from erp.hr.models import Employee

svc = LeaveService()


# ── 산정 룰 (법 조문 기준) ──────────────────────────────────
def test_under_1year_monthly_accrual():
    """§60② 1년 미만 — 7월 입사면 당해 연도 5개월분."""
    days, basis = annual_leave_days(date(2026, 7, 1), 2026)
    assert days == 5.0 and "1년 미만" in basis


def test_first_full_year_15days():
    """§60① 1년 이상 — 15일."""
    days, _ = annual_leave_days(date(2024, 3, 1), 2026)  # 근속 1년+
    assert days == 15.0


def test_3year_service_gets_bonus():
    """§60④ 3년차 — 15 + 1."""
    days, basis = annual_leave_days(date(2023, 1, 1), 2026)  # 근속 3년
    assert days == 16.0 and "가산 1" in basis


def test_cap_at_25days():
    """가산 상한 — 25일."""
    days, _ = annual_leave_days(date(2000, 1, 1), 2026)  # 근속 26년
    assert days == 25.0


# ── 워크플로우 ──────────────────────────────────────────────
async def _emp(session, dept=10):
    e = Employee(employee_no="N001", name="김간호", hired_on=date(2024, 1, 1),
                 department_id=dept, job_group="NURSE")
    session.add(e)
    await session.flush()
    return e


async def test_grant_is_idempotent(session):
    emp = await _emp(session)
    g1 = await svc.grant_annual(session, employee_id=emp.id, year=2026)
    g2 = await svc.grant_annual(session, employee_id=emp.id, year=2026)
    assert g1.id == g2.id and float(g1.granted_days) == 15.0


async def test_request_over_remaining_rejected(session):
    emp = await _emp(session)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    with pytest.raises(ValidationError):
        await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                          start_on=date(2026, 8, 1), end_on=date(2026, 8, 30), days=20)


async def test_approve_deducts_remaining(session):
    emp = await _emp(session)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    req = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                            start_on=date(2026, 8, 1), end_on=date(2026, 8, 3), days=3)
    await svc.decide(session, request_id=req.id, approve=True, decider_user_id=99,
                     decider_dept_id=10, decider_scope_dept_only=True)
    remaining = await svc.remaining_days(session, employee_id=emp.id, year=2026)
    assert remaining == 12.0  # 15 - 3 (E2E ② 단위 버전)


async def test_dept_scope_manager_cannot_approve_other_dept(session):
    """부서장 행 수준 scope — 타 부서 신청 처리 불가 (design §6.2)."""
    emp = await _emp(session, dept=10)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    req = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                            start_on=date(2026, 8, 1), end_on=date(2026, 8, 1), days=1)
    with pytest.raises(ForbiddenError):
        await svc.decide(session, request_id=req.id, approve=True, decider_user_id=99,
                         decider_dept_id=20, decider_scope_dept_only=True)  # 타 부서


async def test_grant_annual_all_wires_active_employees(session):
    """미배선 결함 해소 — grant_annual_all이 재직 전원에 부여 후 연차 신청이 정상 동작(거부 아님)."""
    from erp.hr.models import LeaveGrant
    active = await _emp(session)
    resigned = Employee(employee_no="N002", name="퇴사자", hired_on=date(2024, 1, 1),
                        department_id=10, job_group="NURSE", resigned_on=date(2025, 12, 31))
    session.add(resigned)
    await session.flush()
    out = await svc.grant_annual_all(session, year=2026)
    assert out["granted"] == 1                       # 재직자만(퇴사자 제외)
    # 부여 전이면 거부되던 신청이 이제 통과
    remaining = await svc.remaining_days(session, employee_id=active.id, year=2026)
    assert remaining == 15.0
    req = await svc.request(session, employee_id=active.id, leave_type="ANNUAL",
                            start_on=date(2026, 8, 1), end_on=date(2026, 8, 3), days=3)
    assert req.id is not None
    # 퇴사자는 미부여
    rg = (await session.scalars(select(LeaveGrant).where(
        LeaveGrant.employee_id == resigned.id))).all()
    assert rg == []


async def test_grant_annual_all_idempotent(session):
    """재실행 시 기부여자 스킵(중복 부여 없음)."""
    await _emp(session)
    first = await svc.grant_annual_all(session, year=2026)
    second = await svc.grant_annual_all(session, year=2026)
    assert first["granted"] == 1 and second["granted"] == 0


async def test_double_decide_rejected(session):
    emp = await _emp(session)
    await svc.grant_annual(session, employee_id=emp.id, year=2026)
    req = await svc.request(session, employee_id=emp.id, leave_type="ANNUAL",
                            start_on=date(2026, 8, 1), end_on=date(2026, 8, 1), days=1)
    await svc.decide(session, request_id=req.id, approve=True, decider_user_id=99,
                     decider_dept_id=10, decider_scope_dept_only=True)
    with pytest.raises(ValidationError):
        await svc.decide(session, request_id=req.id, approve=False, decider_user_id=99,
                         decider_dept_id=10, decider_scope_dept_only=True)
