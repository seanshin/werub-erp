"""계정↔직원 신원 판정 — 단일 소스 (v1.288 흐름 검토 5차).

이메일로 직원·계정을 고르고 퇴사 여부를 판정하는 규칙이 SSO·인박스 직원 연결·퇴사 차단·중복 계정
정리 네 곳에 따로 있어, 검토 회차마다 서로 어긋난 경계(재입사자 오차단·표식 차용·중복 정리 우회)가
나왔다. 규칙을 여기 하나로 모으고 호출부는 이 함수만 쓴다.

규칙
1. 이메일 비교는 항상 `strip().lower()`.
2. **이메일로 한 행을 임의로 고르지 않는다.** 재직 중인 직원이 정확히 1명일 때만 그 직원이다
   (재입사로 옛 퇴사 행이 있어도 재직 행이 기준). 재직 행이 2명 이상(공용 이메일)이면 연결하지 않는다.
3. 퇴사 = `resigned_on < 오늘`(퇴사일 당일은 재직). 연결된 계정은 **연결 직원만** 본다. 연결이 없으면
   같은 이메일에 재직 행이 있을 때 퇴사 아님. 연결된 계정을 다른 직원으로 **자동으로 옮기지 않는다** —
   재입사(같은 사람)와 공용 이메일(다른 사람)을 이메일로 구분할 수 없어 관리자가 화면에서 재연결한다.
4. 퇴사자 차단의 예외는 하나 — 그 직원에 **연결된** 계정이고 차단 잡이 그 퇴사를 이미 처리했으면
   (access_revoked_on ≥ resigned_on) 계정의 is_active가 정본(관리자의 의도적 재활성 존중).

common은 hr을 import하지 않는다(계층) — 테이블 직접 조회.
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def norm_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _today() -> str:
    from erp.common.kst import kst_today
    return kst_today().isoformat()


def _active(resigned_on, today: str) -> bool:
    return resigned_on is None or str(resigned_on) >= today


async def active_employee_for_email(session: AsyncSession, email: str | None) -> int | None:
    """재직 중인 직원이 정확히 1명이면 그 id, 아니면 None(0명·공용 이메일 2명 이상)."""
    e = norm_email(email)
    if not e:
        return None
    today = _today()
    rows = (await session.execute(text(
        "SELECT id, resigned_on FROM hr.employees WHERE lower(trim(email)) = :e"), {"e": e})).all()
    active = [r[0] for r in rows if _active(r[1], today)]
    return active[0] if len(active) == 1 else None


async def has_active_employee_for_email(session: AsyncSession, email: str | None,
                                        *, exclude_id: int | None = None) -> bool:
    e = norm_email(email)
    if not e:
        return False
    today = _today()
    rows = (await session.execute(text(
        "SELECT id, resigned_on FROM hr.employees WHERE lower(trim(email)) = :e"), {"e": e})).all()
    return any(_active(r[1], today) for r in rows if r[0] != exclude_id)


async def resolve_link(session: AsyncSession, *, current_employee_id: int | None,
                       email: str | None) -> int | None:
    """계정이 연결될 직원 id — **연결이 없을 때만** 이메일의 유일한 재직 행으로 연결한다.

    이미 연결된 계정은 자동으로 옮기지 않는다(6차 검토). 연결 직원이 퇴사했고 같은 이메일의 재직 행이
    있어도, 그것이 같은 사람의 재입사인지 공용 이메일의 다른 사람인지 이메일로는 구분할 수 없다 —
    자동 재연결하면 퇴사자가 차단을 피하고 남의 급여·연차를 본다. 재연결은 관리자 결정(relink_candidate).
    """
    if current_employee_id is not None:
        return current_employee_id
    return await active_employee_for_email(session, email)


async def relink_candidate(session: AsyncSession, *, employee_id: int | None,
                           email: str | None) -> int | None:
    """연결 직원이 퇴사했고 같은 이메일의 재직 직원이 정확히 1명이면 그 id — 관리자 재연결 후보(재입사?)."""
    if employee_id is None:
        return None
    r = await session.scalar(text("SELECT resigned_on FROM hr.employees WHERE id = :i"),
                             {"i": employee_id})
    if _active(r, _today()):
        return None
    cand = await active_employee_for_email(session, email)
    return cand if cand is not None and cand != employee_id else None


async def resigned_block_reason(session: AsyncSession, *, employee_id: int | None,
                                email: str | None, existing_user: bool) -> str | None:
    """이 계정(또는 신규 JIT)이 퇴사자로 막혀야 하면 사유, 아니면 None. 규칙 3·4.

    - 연결된 계정은 **연결 직원만** 본다(같은 이메일의 다른 재직 행으로 통과시키지 않는다 — 6차 검토).
    - 연결이 없으면 이메일 기준: 재직 행이 있으면 통과(그 직원으로 연결됨), 퇴사 행만 있으면 거부.
    """
    today = _today()
    if employee_id is not None:
        row = (await session.execute(text(
            "SELECT resigned_on, access_revoked_on FROM hr.employees WHERE id = :i"),
            {"i": employee_id})).first()
        if row is None or _active(row[0], today):
            return None
        if existing_user and row[1] is not None and str(row[1]) >= str(row[0]):
            return None                                    # 처리된 퇴사 + 연결 계정 → is_active가 정본
        if await relink_candidate(session, employee_id=employee_id, email=email) is not None:
            return ("퇴사 처리된 직원 기록에 연결된 계정입니다 — 재입사라면 ERP 관리자가 사용자 관리에서 "
                    "'재직 기록으로 재연결'을 해야 합니다")
        return "퇴사 처리된 직원입니다 — 인사팀에 문의하세요"
    if await has_active_employee_for_email(session, email):
        return None
    e = norm_email(email)
    rows = (await session.execute(text(
        "SELECT resigned_on FROM hr.employees WHERE lower(trim(email)) = :e"), {"e": e})).all() if e else []
    if rows:                                               # 이메일로만 퇴사자와 맞음(연결 없음) → 거부
        return "퇴사 처리된 직원입니다 — 인사팀에 문의하세요"
    return None
