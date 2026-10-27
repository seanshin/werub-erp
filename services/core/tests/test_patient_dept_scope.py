"""환자 접수·예약 목록 부서 경계 (security-architect MEDIUM — scope 반환 후 폐기 회귀).

MANAGER(DEPT 스코프)는 본부서 접수/예약만 조회 가능. ADMIN(ALL)은 전체.
이전: _require로 scope를 얻고도 WHERE에 합성하지 않아 전 부서 chart_no·환자명 노출.
"""
from datetime import date, datetime, timezone

from sqlalchemy import select

from erp.common.router import CurrentUser
from erp.patient.models import Patient, Reception, Reservation
from erp.patient.router import list_receptions


def _mgr(dept):
    return CurrentUser(user_id=100 + dept, role="MANAGER", dept_id=dept, license_flag=True)


_ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
_ON = date(2026, 6, 1)


async def _seed(session):
    from erp.common.models import Department
    # dept_id(PK) → dept_code 매핑: receptions.dept_code는 진료과코드 체계(prod="IM")
    d1 = Department(name="내과", code="1")
    d2 = Department(name="외과", code="2")
    session.add_all([d1, d2])
    await session.flush()
    pa = Patient(chart_no="C-A", name="환자A", birth_date=date(1990, 1, 1))
    pb = Patient(chart_no="C-B", name="환자B", birth_date=date(1991, 2, 2))
    session.add_all([pa, pb])
    await session.flush()
    session.add_all([
        Reception(patient_id=pa.id, dept_code="1", received_on=_ON),   # d1.code
        Reception(patient_id=pb.id, dept_code="2", received_on=_ON),   # d2.code
        Reservation(patient_id=pa.id, dept_code="1", doctor_id=11,
                    reserved_at=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)),
        Reservation(patient_id=pb.id, dept_code="2", doctor_id=22,
                    reserved_at=datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)),
    ])
    await session.flush()
    return d1, d2


async def test_receptions_dept_scoped(session):
    d1, d2 = await _seed(session)
    # MANAGER 내과 — 본부서 접수만(dept_id→code "1" 해소)
    mine = await list_receptions(on=_ON, user=_mgr(d1.id), session=session)
    assert {r["dept_code"] for r in mine["items"]} == {"1"}
    assert {r["chart_no"] for r in mine["items"]} == {"C-A"}
    # MANAGER 외과 — 본부서 접수만 (타 부서 미노출 교차검증)
    other = await list_receptions(on=_ON, user=_mgr(d2.id), session=session)
    assert {r["chart_no"] for r in other["items"]} == {"C-B"}
    # ADMIN(ALL) — 전체
    allr = await list_receptions(on=_ON, user=_ADMIN, session=session)
    assert {r["dept_code"] for r in allr["items"]} == {"1", "2"}


async def test_reservation_status_transition(session):
    """예약 상태전이 — BOOKED→VISITED 성공·종결 후 재전이/무효전이 거부·부서경계."""
    import pytest

    from erp.errors import ForbiddenError, ValidationError
    from erp.patient.router import ReservationStatusBody, update_reservation_status
    d1, d2 = await _seed(session)
    rid = (await session.scalars(
        select(Reservation.id).where(Reservation.dept_code == "1"))).first()
    # BOOKED → VISITED (방문 처리)
    out = await update_reservation_status(rid, ReservationStatusBody(status="VISITED"),
                                          user=_ADMIN, session=session)
    assert out["status"] == "VISITED"
    # 종결 상태에서 재전이 불가
    with pytest.raises(ValidationError):
        await update_reservation_status(rid, ReservationStatusBody(status="NOSHOW"),
                                        user=_ADMIN, session=session)
    # 부서 경계 — 외과 MANAGER가 내과 예약 처리 불가
    r2 = (await session.scalars(
        select(Reservation.id).where(Reservation.dept_code == "1"))).first()
    with pytest.raises(ForbiddenError):
        await update_reservation_status(r2, ReservationStatusBody(status="CANCELED"),
                                        user=_mgr(d2.id), session=session)


async def test_patient_search_dept_scoped(session):
    """환자 검색 부서 경계(§6.2 P0-3) — MANAGER는 본부서 접수 환자만, ADMIN은 전체."""
    from erp.patient.router import search_patients
    d1, _ = await _seed(session)
    mine = await search_patients(user=_mgr(d1.id), session=session)   # 내과 MANAGER
    assert {p["chart_no"] for p in mine["items"]} == {"C-A"}          # 타부서 환자(C-B) 미노출
    allp = await search_patients(user=_ADMIN, session=session)
    assert {"C-A", "C-B"} <= {p["chart_no"] for p in allp["items"]}   # ADMIN 전체


# 참고: list_reservations 도 동일한 `scope.kind==DEPT → Reservation.dept_code` WHERE
# 패턴으로 가드한다(코드 1:1 동일). SQLite는 CAST(datetime AS DATE) 일자 매칭을
# 지원하지 않아(PG 전용) 라이브 단위테스트 불가 — 통합환경에서 커버하며, 위 receptions
# 테스트가 동일 스코프 필터 로직을 E2E로 검증한다.
