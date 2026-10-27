"""퇴사자 ERP 로그인 차단 — 퇴사일이 지난 직원의 연결 계정을 비활성·세션 폐기.

종전엔 HIS staff.resigned가 인사 레코드(resigned_on)·4대보험 상실만 반영하고 **ERP 계정은 그대로**
두었다. HIS 계정이 막혀 SSO는 끊겨도, 발급된 갱신 토큰(최대 7일)·PIN/패스키·비밀번호 로그인으로
퇴사자가 계속 들어올 수 있었다.

- 수신 즉시: 퇴사일이 이미 지났으면 바로 차단(inbound_consumer).
- 매일: 퇴사일이 지난 직원을 차단(정기업무 hr.resigned_access_revoke) — 퇴사마다 1회.
- ADMIN도 예외 없이 막는다(퇴사자에게 관리 권한이 남는 게 가장 나쁘다) — 단 **마지막 활성 ADMIN이면
  막지 않고 경고**(관리 화면 락아웃 방지·부트스트랩 CLI로 대체 가능).
"""
import logging
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import User
from erp.hr.models import Employee

logger = logging.getLogger("erp.hr.access_revoke")


def _auth():
    from erp.common.auth_service import AuthService
    from erp.common.redis_client import get_redis
    return AuthService(redis=get_redis())


async def revoke_for_employee(session: AsyncSession, emp: Employee, *, today: date) -> int:
    """퇴사일 **다음 날**부터 연결 계정 비활성 — 그 퇴사에 대해 1회만(access_revoked_on). 반환=차단 수.

    - 퇴사일 당일은 허용(resigned_on이 마지막 근무일인 경우 마지막 날 접속을 막지 않는다).
    - 이미 이 퇴사로 차단했으면(access_revoked_on ≥ resigned_on) 다시 막지 않는다 — 관리자가 일부러
      다시 켠 계정(자문·재계약)을 매일 되돌리던 문제(G9).
    - 연결 계정 = user.employee_id. 연결이 하나도 없을 때만 같은 이메일로 찾는다(F3 — 공용 이메일 오차단 방지).
    - 마지막 활성 ADMIN이면 막지 않고 표식도 남기지 않는다(다음 날 재시도·락아웃 방지).
    """
    if emp.resigned_on is None or emp.resigned_on >= today:
        return 0
    if emp.access_revoked_on is not None and emp.access_revoked_on >= emp.resigned_on:
        return 0
    users = (await session.scalars(select(User).where(User.employee_id == emp.id))).all()
    if not users and emp.email:
        # 같은 이메일의 재직 중인 직원(재입사 등)이 있으면 이메일로 찾지 않는다(D2·common.identity 규칙 3)
        from erp.common.identity import has_active_employee_for_email, norm_email
        if not await has_active_employee_for_email(session, emp.email, exclude_id=emp.id):
            # 다른 직원에 연결된 계정은 잡지 않는다(규칙 3 — 연결된 계정은 연결 직원만 본다·7차 검토)
            users = (await session.scalars(select(User).where(
                func.lower(func.trim(User.email)) == norm_email(emp.email),
                User.employee_id.is_(None)))).all()
    n = 0
    skipped = False
    for u in users:
        if not u.is_active:
            continue
        if u.role == "ADMIN":
            admins = await session.scalar(select(func.count()).select_from(User).where(
                User.role == "ADMIN", User.is_active.is_(True)))
            if (admins or 0) <= 1:
                logger.warning("퇴사자 계정 차단 보류 — 마지막 활성 ADMIN(락아웃 방지)",
                               extra={"ctx": {"user_id": u.id}})
                skipped = True
                continue
        await _auth().deactivate(session, user=u)   # refresh 폐기·tv 증가·캐시 삭제(G1)
        n += 1
        # 같은 이메일의 재직 직원이 있으면 재입사일 수 있다 — 차단은 하되(동일인 확인 전 fail-closed)
        # 관리자에게 알려 사용자 관리에서 재연결·활성화하게 한다(6차 검토).
        from erp.common.identity import relink_candidate
        cand = await relink_candidate(session, employee_id=emp.id, email=emp.email or u.email)
        if cand is not None:
            from erp.common.models import Notification
            for aid in (await session.scalars(select(User.id).where(
                    User.role == "ADMIN", User.is_active.is_(True)))).all():
                session.add(Notification(
                    user_id=aid, type="ACCOUNT_RELINK_REVIEW", title="재입사 의심 계정 확인",
                    body=f"퇴사 처리로 차단한 계정 #{u.id}과 같은 이메일의 재직 직원(#{cand})이 있습니다 — "
                         "같은 사람이면 사용자 관리에서 '재직 기록으로 재연결'을 실행하세요"))
        logger.info("퇴사자 ERP 계정 차단", extra={"ctx": {"user_id": u.id, "employee_id": emp.id}})
    # 표식은 **실제로 계정을 확인했을 때만** — 연결 계정이 하나도 없을 때 찍으면, 뒤에 SSO로 생긴
    # 계정이 영구히 차단되지 않았다(H1). 계정이 없으면 매일 다시 본다(값싼 조회).
    if users and not skipped:
        emp.access_revoked_on = today
    await session.flush()
    return n


async def revoke_due(session: AsyncSession, *, today: date) -> dict:
    """퇴사일이 지났고 이 퇴사로 아직 차단하지 않은 직원 — 일일 정기업무(첫 실행=과거분 따라잡기)."""
    emps = (await session.scalars(select(Employee).where(
        Employee.resigned_on.isnot(None), Employee.resigned_on < today,
        or_(Employee.access_revoked_on.is_(None),
            Employee.access_revoked_on < Employee.resigned_on)))).all()
    revoked = 0
    for emp in emps:
        revoked += await revoke_for_employee(session, emp, today=today)
    return {"checked": len(emps), "revoked": revoked}
