"""그룹웨어(Clinic) 근태 REST 어댑터·수집·집계 (M0a=REST, FR-28/29).

검증: SSRF 화이트리스트·인증 헤더·응답 파싱 → staging 멱등 적재 → 사번 해소·월 집계.
"""
from datetime import date, datetime, timezone

import httpx
import pytest
from sqlalchemy import select

from erp.hr.attendance import consume_attendance_staging
from erp.hr.models import (
    AttendanceSummary,
    DutyAssignment,
    DutySchedule,
    Employee,
    GroupwareAttendanceStaging,
)
from erp.integrations.groupware import (
    GroupwareAttendanceCollector,
    RestGroupwareAttendanceSource,
)

UTC = timezone.utc


def _mock_source(records: list[dict]) -> RestGroupwareAttendanceSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Hospital-Code"] == "WRB-DEMO-M66M"
        assert request.headers["authorization"] == "Bearer secret-k"
        assert request.url.path == "/api/clinic/his/attendance"
        return httpx.Response(200, json={"data": {"records": records}})

    return RestGroupwareAttendanceSource(
        "https://clinic.test", "secret-k", "WRB-DEMO-M66M",
        allowed_hosts=frozenset({"clinic.test"}),
        transport=httpx.MockTransport(handler))


# ── 어댑터: SSRF·파싱 ────────────────────────────────────────
def test_ssrf_guard_rejects_unlisted_host():
    with pytest.raises(ValueError, match="허용되지 않은"):
        RestGroupwareAttendanceSource("https://evil.example", "k", "H")


def test_fetch_parses_records_and_headers():
    src = _mock_source([
        {"userId": "clinic-1", "name": "김의사", "department": "내과",
         "status": "checked_out", "checkedInAt": "2026-05-06T08:30:00Z",
         "checkedOutAt": "2026-05-06T18:10:00Z", "employeeNo": "E0001"},
        {"userId": "clinic-2", "name": "이간호", "department": "간호부",
         "status": "checked_in", "checkedInAt": "2026-05-06T22:05:00Z",
         "checkedOutAt": None},  # employeeNo 없음 → userId 폴백
    ])
    dtos = list(src.fetch_attendance(datetime(2026, 5, 5, tzinfo=UTC)))
    assert len(dtos) == 2
    assert dtos[0].employee_no == "E0001"          # 명시 사번 우선
    assert dtos[0].work_on == date(2026, 5, 6)
    assert dtos[0].clock_out.hour == 18
    assert dtos[1].employee_no == "clinic-2"       # 폴백: userId
    assert dtos[1].external_user_id == "clinic-2"
    assert dtos[1].clock_out is None


def test_fetch_skips_record_without_user_id():
    src = _mock_source([{"name": "무명", "checkedInAt": "2026-05-06T09:00:00Z"}])
    assert list(src.fetch_attendance(datetime(2026, 5, 5, tzinfo=UTC))) == []


# ── 수집: 멱등 적재 ──────────────────────────────────────────
class _StubSource:
    def __init__(self, dtos):
        self._dtos = dtos

    def fetch_attendance(self, since):
        return iter(self._dtos)


async def test_collect_is_idempotent(session):
    src = _mock_source([
        {"userId": "clinic-1", "checkedInAt": "2026-05-06T08:30:00Z",
         "checkedOutAt": "2026-05-06T17:00:00Z", "employeeNo": "E0001"},
    ])
    dtos = list(src.fetch_attendance(datetime(2026, 5, 5, tzinfo=UTC)))
    collector = GroupwareAttendanceCollector(_StubSource(dtos))

    first = await collector.collect(session, since=datetime(2026, 5, 5, tzinfo=UTC))
    second = await collector.collect(session, since=datetime(2026, 5, 5, tzinfo=UTC))
    assert first == {"collected": 1, "duplicates": 0}
    assert second == {"collected": 0, "duplicates": 1}  # source_ref 멱등
    rows = (await session.execute(select(GroupwareAttendanceStaging))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "NEW"


# ── 소비: 사번 해소·월 집계 ──────────────────────────────────
async def _seed_employee(session, emp_no="E0001"):
    emp = Employee(employee_no=emp_no, name="홍길동", hired_on=date(2020, 1, 1))
    session.add(emp)
    await session.flush()
    return emp


async def test_consume_aggregates_and_resolves(session):
    emp = await _seed_employee(session)
    session.add_all([
        GroupwareAttendanceStaging(
            source_ref="clinic-1:2026-05-06", external_user_id="clinic-1", employee_no="E0001",
            work_on=date(2026, 5, 6), status="NEW",
            clock_in=datetime(2026, 5, 6, 8, 0),
            clock_out=datetime(2026, 5, 6, 17, 0)),          # 9h = 540m
        GroupwareAttendanceStaging(
            source_ref="clinic-1:2026-05-07", external_user_id="clinic-1", employee_no="E0001",
            work_on=date(2026, 5, 7), status="NEW",
            clock_in=datetime(2026, 5, 7, 22, 0),            # 야간
            clock_out=datetime(2026, 5, 8, 6, 0)),          # 8h = 480m
    ])
    await session.flush()

    result = await consume_attendance_staging(session)
    assert result == {"applied": 2, "summaries": 1, "unmapped": 0}

    summ = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id))).scalar_one()
    assert summ.yyyymm == "2026-05"
    assert summ.work_days == 2
    assert summ.total_work_minutes == 540 + 480
    assert summ.night_shift_count == 1
    # 소비 후 staging은 APPLIED, 재소비 시 no-op (멱등)
    again = await consume_attendance_staging(session)
    assert again == {"applied": 0, "summaries": 0, "unmapped": 0}


async def test_consume_late_early_via_duty(session):
    """H-1: 듀티 교대(D 07:00–15:00) 대비 지각·조퇴 산정."""
    emp = await _seed_employee(session)
    sched = DutySchedule(department_id=1, yyyymm="2026-05")
    session.add(sched)
    await session.flush()
    session.add(DutyAssignment(schedule_id=sched.id, employee_id=emp.id,
                               duty_on=date(2026, 5, 6), shift="D"))
    session.add(GroupwareAttendanceStaging(
        source_ref="clinic-1:2026-05-06", external_user_id="clinic-1", employee_no="E0001",
        work_on=date(2026, 5, 6), status="NEW",
        clock_in=datetime(2026, 5, 6, 7, 30),    # 07:10 유예 초과 → 지각
        clock_out=datetime(2026, 5, 6, 14, 30)))  # 14:50 이전 → 조퇴
    await session.flush()

    await consume_attendance_staging(session)
    summ = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id))).scalar_one()
    assert summ.late_count == 1 and summ.early_leave_count == 1
    assert summ.work_days == 1


async def test_consume_custom_shift_judged_via_catalog(session):
    """커스텀 시프트(당직 OC)도 shift_types 등록 시각·야간으로 지각/조퇴·야간 판정(전체검토 4)."""
    from datetime import time

    from erp.hr.models import ShiftType
    emp = await _seed_employee(session)
    session.add(ShiftType(code="OC", label="당직", work_hours=12, is_night=True,
                          counts_as_workday=True, start_time=time(8, 0), end_time=time(20, 0)))
    sched = DutySchedule(department_id=1, yyyymm="2026-05")
    session.add(sched)
    await session.flush()
    session.add(DutyAssignment(schedule_id=sched.id, employee_id=emp.id,
                               duty_on=date(2026, 5, 6), shift="OC"))
    session.add(GroupwareAttendanceStaging(
        source_ref="clinic-1:2026-05-06", external_user_id="clinic-1", employee_no="E0001",
        work_on=date(2026, 5, 6), status="NEW",
        clock_in=datetime(2026, 5, 6, 8, 30),     # 08:10 유예 초과 → 지각
        clock_out=datetime(2026, 5, 6, 19, 30)))  # 19:50 이전 → 조퇴
    await session.flush()
    await consume_attendance_staging(session)
    summ = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id))).scalar_one()
    assert summ.late_count == 1 and summ.early_leave_count == 1
    assert summ.night_shift_count == 1     # OC.is_night=True (카탈로그 야간 플래그)


async def test_consume_no_duty_no_late_early(session):
    """듀티 미배정이면 지각/조퇴 판정 불가 → 0 (집계 골격은 동작)."""
    emp = await _seed_employee(session)
    session.add(GroupwareAttendanceStaging(
        source_ref="clinic-1:2026-05-06", external_user_id="clinic-1", employee_no="E0001",
        work_on=date(2026, 5, 6), status="NEW",
        clock_in=datetime(2026, 5, 6, 9, 0), clock_out=None))
    await session.flush()

    await consume_attendance_staging(session)
    summ = (await session.execute(
        select(AttendanceSummary).where(AttendanceSummary.employee_id == emp.id))).scalar_one()
    assert summ.late_count == 0 and summ.early_leave_count == 0 and summ.work_days == 1


async def test_consume_marks_unmapped_failed(session):
    session.add(GroupwareAttendanceStaging(
        source_ref="ghost:2026-05-06", external_user_id="ghost", employee_no="E9999",
        work_on=date(2026, 5, 6), status="NEW",
        clock_in=datetime(2026, 5, 6, 9, 0)))
    await session.flush()

    result = await consume_attendance_staging(session)
    assert result == {"applied": 0, "summaries": 0, "unmapped": 1}
    row = (await session.execute(select(GroupwareAttendanceStaging))).scalar_one()
    assert row.status == "FAILED"
    assert row.error_detail["reason"] == "unmapped_employee_no"


def test_aggregate_pg_roundtrip_true_utc_aware():
    """PG(asyncpg) 레인 회귀 — 되읽힌 clock은 진짜 UTC aware(v1.257.2 prod 실측).

    KST 22:00 출근(야간)=13:00 UTC·KST 07:00 출근(주간)=전일 22:00 UTC. 라벨-strip(-9h 왜곡)이면
    주간 출근이 22시로 읽혀 야간 오검출·야간 출근은 13시로 읽혀 미검출된다(as_kst_wallclock 회귀).
    """
    from types import SimpleNamespace

    from erp.hr.attendance import _aggregate
    day = SimpleNamespace(  # KST 07:00~16:00 주간 근무 — 라벨-strip이면 ci=22:00로 야간 오검출
        work_on=date(2026, 5, 6),
        clock_in=datetime(2026, 5, 5, 22, 0, tzinfo=UTC),
        clock_out=datetime(2026, 5, 6, 7, 0, tzinfo=UTC))
    night = SimpleNamespace(  # KST 22:00~익일 06:00 야간 — 라벨-strip이면 ci=13:00로 미검출
        work_on=date(2026, 5, 7),
        clock_in=datetime(2026, 5, 7, 13, 0, tzinfo=UTC),
        clock_out=datetime(2026, 5, 7, 21, 0, tzinfo=UTC))
    out = _aggregate([day, night], duty_by={}, catalog={})
    assert out["work_days"] == 2 and out["night_shift_count"] == 1
    assert out["total_work_minutes"] == 540 + 480
