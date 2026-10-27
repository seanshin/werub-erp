"""퇴사자 ERP 계정 차단 — HIS staff.resigned·퇴사일 경과."""
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from erp.common.kst import kst_today
from erp.common.models import User
from erp.errors import TokenError
from erp.hr.access_revoke import revoke_due
from erp.hr.models import Employee


async def _emp(session, no, *, email, resigned_on=None, source="HIS"):
    e = Employee(employee_no=no, name=no, department_id=1, hired_on=date(2020, 1, 1),
                 job_group="NURSE", email=email, source=source, resigned_on=resigned_on)
    session.add(e)
    await session.flush()
    return e


async def _user(session, email, role="STAFF", employee_id=None):
    u = User(email=email, role=role, password_hash="x", is_active=True, token_version=0,
             employee_id=employee_id)
    session.add(u)
    await session.flush()
    return u


async def test_resigned_event_revokes_login(session):
    """staff.resigned(퇴사일 어제) 수신 → 같은 이메일 계정 비활성·토큰 무효."""
    from erp.integrations.inbound_consumer import _staff
    await _emp(session, "R-1", email="r1@h.example")
    u = await _user(session, "r1@h.example")
    await _staff(session, {"employeeNumber": "R-1", "email": "r1@h.example",
                           "resignDate": (kst_today() - timedelta(days=1)).isoformat()},
                 "staff.resigned", {})
    await session.refresh(u)
    assert u.is_active is False and u.token_version == 1


async def test_resign_day_itself_still_allowed(session):
    """퇴사일 당일은 허용 — 마지막 근무일 접속을 막지 않는다."""
    from erp.integrations.inbound_consumer import _staff
    await _emp(session, "R-0", email="r0@h.example")
    u = await _user(session, "r0@h.example")
    await _staff(session, {"employeeNumber": "R-0", "email": "r0@h.example",
                           "resignDate": kst_today().isoformat()}, "staff.resigned", {})
    await session.refresh(u)
    assert u.is_active is True
    assert (await revoke_due(session, today=kst_today() + timedelta(days=1)))["revoked"] == 1


async def test_future_resign_waits_then_daily_job_revokes(session):
    from erp.integrations.inbound_consumer import _staff
    future = kst_today() + timedelta(days=10)
    await _emp(session, "R-2", email="r2@h.example")
    u = await _user(session, "r2@h.example")
    await _staff(session, {"employeeNumber": "R-2", "email": "r2@h.example",
                           "resignDate": future.isoformat()}, "staff.resigned", {})
    await session.refresh(u)
    assert u.is_active is True                             # 아직 재직
    out = await revoke_due(session, today=future + timedelta(days=1))
    await session.refresh(u)
    assert out["revoked"] == 1 and u.is_active is False


async def test_admin_reenable_is_not_undone_next_day(session):
    """G9 — 같은 퇴사로는 1회만 차단. 관리자가 다시 켠 계정은 다음 날 그대로."""
    e = await _emp(session, "R-5", email="r5@h.example", resigned_on=kst_today() - timedelta(days=3))
    u = await _user(session, "r5@h.example", employee_id=e.id)
    assert (await revoke_due(session, today=kst_today()))["revoked"] == 1
    u.is_active = True                                     # 관리자 재활성화(자문 재계약 등)
    await session.flush()
    assert (await revoke_due(session, today=kst_today() + timedelta(days=1)))["revoked"] == 0
    await session.refresh(u)
    assert u.is_active is True


async def test_email_match_only_without_linked_account(session):
    """F3 — 직원에 연결 계정이 있으면 그것만. 같은 이메일의 다른 계정은 건드리지 않는다."""
    e = await _emp(session, "R-6", email="shared@h.example", resigned_on=kst_today() - timedelta(days=1))
    linked = await _user(session, "linked@h.example", employee_id=e.id)
    other = await _user(session, "shared@h.example")
    await revoke_due(session, today=kst_today())
    await session.refresh(linked)
    await session.refresh(other)
    assert linked.is_active is False and other.is_active is True


async def test_last_admin_not_locked_out(session):
    e = await _emp(session, "R-3", email="boss@h.example", resigned_on=kst_today() - timedelta(days=1))
    u = await _user(session, "boss@h.example", role="ADMIN", employee_id=e.id)
    out = await revoke_due(session, today=kst_today())
    await session.refresh(u)
    assert out["revoked"] == 0 and u.is_active is True     # 마지막 ADMIN — 경고만·표식 없음
    await _user(session, "other-admin@h.example", role="ADMIN")
    out = await revoke_due(session, today=kst_today())
    await session.refresh(u)
    assert out["revoked"] == 1 and u.is_active is False


async def test_active_employee_untouched(session):
    await _emp(session, "A-1", email="a1@h.example")
    u = await _user(session, "a1@h.example")
    assert (await revoke_due(session, today=kst_today()))["revoked"] == 0
    assert (await session.scalar(select(User.is_active).where(User.id == u.id))) is True


async def test_revoked_user_cannot_refresh(session):
    """G1 — 차단 후 남아 있던 refresh 쿠키로 세션을 다시 받지 못한다(종전: 7일간 재발급)."""
    from erp.common.auth_service import AuthService
    e = await _emp(session, "R-7", email="r7@h.example", resigned_on=kst_today() - timedelta(days=1))
    u = await _user(session, "r7@h.example", employee_id=e.id)
    tokens = await AuthService()._issue_tokens(session, u, family_id="fam-r7")
    await revoke_due(session, today=kst_today())
    with pytest.raises(TokenError):
        await AuthService().refresh(session, raw_refresh=tokens["refresh_token"])


async def test_admin_deactivate_blocks_refresh(session):
    """G1 — 관리화면 비활성도 같은 경로(refresh 폐기)."""
    from erp.common.auth_service import AuthService
    u = await _user(session, "deact@h.example")
    tokens = await AuthService()._issue_tokens(session, u, family_id="fam-d")
    await AuthService().deactivate(session, user=u)
    with pytest.raises(TokenError):
        await AuthService().refresh(session, raw_refresh=tokens["refresh_token"])


async def test_no_marker_without_accounts_so_later_account_is_caught(session):
    """H1 — 계정이 없을 때 표식을 찍지 않는다. 뒤에 생긴(연결된) 계정도 다음 실행에서 차단."""
    e = await _emp(session, "R-8", email="r8@h.example", resigned_on=kst_today() - timedelta(days=1))
    await revoke_due(session, today=kst_today())
    assert e.access_revoked_on is None
    u = await _user(session, "r8@h.example", employee_id=e.id)
    assert (await revoke_due(session, today=kst_today()))["revoked"] == 1
    await session.refresh(u)
    assert u.is_active is False


async def test_email_fallback_skips_when_rehired_row_exists(session):
    """D2 — 옛 퇴사 행의 이메일 폴백이 같은 이메일의 재입사자 계정을 막지 않는다."""
    await _emp(session, "OLD-1", email="back@h.example", resigned_on=kst_today() - timedelta(days=300))
    await _emp(session, "NEW-1", email="back@h.example")
    u = await _user(session, "back@h.example")          # 연결 없음(SSO 연결 전)
    assert (await revoke_due(session, today=kst_today()))["revoked"] == 0
    await session.refresh(u)
    assert u.is_active is True


async def test_revoke_with_rehire_candidate_notifies_admin(session):
    """6차 — 재입사 후보가 있어도 차단은 하되(fail-closed) 관리자에게 재연결 확인 알림."""
    from erp.common.models import Notification
    await _user(session, "adm-n@h.example", role="ADMIN")
    old = await _emp(session, "RO-1", email="ro@h.example", resigned_on=kst_today() - timedelta(days=1))
    await _emp(session, "RO-2", email="ro@h.example")
    u = await _user(session, "ro@h.example", employee_id=old.id)
    assert (await revoke_due(session, today=kst_today()))["revoked"] == 1
    await session.refresh(u)
    assert u.is_active is False
    n = (await session.execute(select(Notification).where(
        Notification.type == "ACCOUNT_RELINK_REVIEW"))).scalars().all()
    assert len(n) == 1


async def test_email_fallback_ignores_accounts_linked_elsewhere(session):
    """7차 — 이메일 폴백은 다른 직원에 연결된 계정을 잡지 않는다(연결 계정은 연결 직원만)."""
    other = await _emp(session, "LK-2", email="lk@h.example")
    await _emp(session, "LK-1", email="lk@h.example", resigned_on=kst_today() - timedelta(days=1))
    u = await _user(session, "lk@h.example", employee_id=other.id)
    await revoke_due(session, today=kst_today())
    await session.refresh(u)
    assert u.is_active is True


async def test_new_hire_with_account_on_old_row_notifies_admin(session):
    """7차 — 퇴사→차단→몇 달 뒤 재입사: 새 직원 생성 시 재연결 확인 알림."""
    from erp.common.models import Notification
    from erp.integrations.inbound_consumer import _staff
    await _user(session, "adm-r@h.example", role="ADMIN")
    old = await _emp(session, "RH-OLD", email="later@h.example", resigned_on=kst_today() - timedelta(days=90))
    await _user(session, "later@h.example", employee_id=old.id)
    await _staff(session, {"employeeNumber": "RH-NEW", "email": "later@h.example", "name": "later"},
                 "staff.created", {})
    n = (await session.execute(select(Notification).where(
        Notification.type == "ACCOUNT_RELINK_REVIEW"))).scalars().all()
    assert len(n) == 1
