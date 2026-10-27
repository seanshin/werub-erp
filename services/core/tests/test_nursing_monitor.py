"""간호등급 모니터링 (P3) — 재직일수 자동(휴직 제외)·추정 재원·시뮬레이션."""
from datetime import date

from erp.hr.models import Employee, LeaveAbsence
from erp.integrations.inbound_consumer import _billing_line
from erp.regulatory.nursing_monitor import NursingMonitor

mon = NursingMonitor()
START, END = date(2026, 3, 15), date(2026, 6, 14)   # 92일 (3/15~6/14)


def _nurse(no, hired=date(2025, 1, 1), resigned=None):
    return Employee(employee_no=no, name=f"간호{no}", hired_on=hired, resigned_on=resigned,
                    job_group="NURSE", job_category="NURSE", employment_type="REGULAR",
                    source="ERP")


async def test_nurse_days_with_absence_exclusion(session):
    period_days = (END - START).days + 1
    full = _nurse("N1")                                   # 전기간
    mid = _nurse("N2", hired=date(2026, 5, 1))            # 5/1~6/14 = 45일
    gone = _nurse("N3", resigned=date(2026, 3, 31))       # 3/15~3/31 = 17일
    on_leave = _nurse("N4")                               # 휴직 30일 제외
    short_abs = _nurse("N5")                              # 휴직 10일(<16) → 미제외
    session.add_all([full, mid, gone, on_leave, short_abs])
    await session.flush()
    session.add_all([
        LeaveAbsence(employee_id=on_leave.id, kind="PARENTAL",
                     start_on=date(2026, 4, 1), return_on=date(2026, 5, 1)),   # 4/1~4/30
        LeaveAbsence(employee_id=short_abs.id, kind="SICK",
                     start_on=date(2026, 4, 1), return_on=date(2026, 4, 11)),  # 10일
    ])
    await session.flush()

    nd = await mon.nurse_days(session, start=START, end=END)
    by = {n["employee_no"]: n for n in nd["nurses"]}
    assert by["N1"]["days"] == period_days
    assert by["N2"]["days"] == 45 and by["N3"]["days"] == 17
    assert by["N4"]["days"] == period_days - 30
    assert by["N4"]["excluded_absence_days"] == 30
    assert by["N5"]["days"] == period_days                 # 16일 미만 부재 미제외
    assert nd["headcount"] == 5


async def test_nurse_days_fte_and_exclusion(session):
    """주 근로시간 단시간 환산(0.4~0.9) + 산정 제외 사유 반영."""
    period_days = (END - START).days + 1
    full = _nurse("F1")                       # 전일제 → 1.0
    part = _nurse("P1")
    part.weekly_hours = 24                     # 24h → 0.6
    excl = _nurse("X1")
    excl.nursing_exclude_reason = "간호감독"    # 산정 제외
    session.add_all([full, part, excl])
    await session.flush()

    nd = await mon.nurse_days(session, start=START, end=END)
    by = {n["employee_no"]: n for n in nd["nurses"]}
    assert by["F1"]["fte"] == 1.0 and by["F1"]["days"] == period_days
    assert by["P1"]["fte"] == 0.6
    assert by["P1"]["days"] == round(period_days * 0.6, 2)
    assert "X1" not in by and nd["headcount"] == 2         # 제외자는 인원에서 빠짐
    assert nd["excluded_from_count"][0]["reason"] == "간호감독"


async def test_monitor_with_simulation(session):
    for i in range(10):                                    # 간호사 10명 전기간
        session.add(_nurse(f"NS{i}"))
    await session.flush()
    days = (END - START).days + 1
    out = await mon.monitor(session, hospital_type="HOSPITAL",
                            patient_days=24.9 * days, as_of=date(2026, 8, 1))
    assert out["period"]["start"] == START and out["period"]["end"] == END
    assert out["avg_nurses"] == 10.0
    assert out["ratio"] == 2.49 and out["grade"] == "1"    # HOSPITAL: <2.5 → 1등급
    assert out["patient_days_source"] == "MANUAL"
    # +3명(13명)이면 24.9/13=1.91 < 2.0 → A등급 진입
    assert out["simulation"]["upgrade_nurses_needed"] == 3
    assert out["simulation"]["grade_if_one_leaves"] == "2"  # 9명 → 2.76 → 2등급


async def test_patient_days_estimate_from_billing_lines(session):
    for i, d in enumerate((1, 1, 2)):                      # 같은 날 중복 라인은 1일 처리
        await _billing_line(session, {
            "lineId": f"L-NG-{i}", "chartNo": "C-NG", "code": "AA100",
            "encounterType": "INPATIENT", "treatedOn": f"2026-04-0{d}"},
            "billing.line_recorded", {})
    est = await mon.patient_days_estimate(session, start=START, end=END)
    assert est == 2                                        # distinct(차트,일자)


async def test_long_leave_crossing_window_boundary_excluded(session):
    """장기 휴직이 산정창 종료 직전 시작 → 창 겹침은 며칠뿐이나 연속 부재 16일+라 제외(적대검토 MED).

    종전: 창 겹침일수(ov)로 임계 판정해 12일<16으로 오판, 전일제 재직 계상.
    수정: 부재 실제 길이(90일)로 임계 판정, 차감은 창 겹침분(12일)만.
    """
    period_days = (END - START).days + 1
    nurse = _nurse("L1")
    session.add(nurse)
    await session.flush()
    # 6/3 시작·90일 휴직(9/1 복귀) → 창(3/15~6/14)엔 6/3~6/14 = 12일만 겹침
    session.add(LeaveAbsence(employee_id=nurse.id, kind="PARENTAL",
                             start_on=date(2026, 6, 3), return_on=date(2026, 9, 1)))
    await session.flush()
    nd = await mon.nurse_days(session, start=START, end=END)
    n = next(x for x in nd["nurses"] if x["employee_no"] == "L1")
    assert n["excluded_absence_days"] == 12                 # 창 겹침분만 차감
    assert n["days"] == period_days - 12                    # 제외 반영(종전 버그: period_days)


async def test_open_ended_leave_excluded(session):
    """복귀일 미정(무기한 휴직)은 임계 충족으로 간주 — 창 겹침분 차감."""
    period_days = (END - START).days + 1
    nurse = _nurse("O1")
    session.add(nurse)
    await session.flush()
    session.add(LeaveAbsence(employee_id=nurse.id, kind="SICK",
                             start_on=date(2026, 6, 1), return_on=None))   # 6/1~창끝 = 14일
    await session.flush()
    nd = await mon.nurse_days(session, start=START, end=END)
    n = next(x for x in nd["nurses"] if x["employee_no"] == "O1")
    assert n["excluded_absence_days"] == 14 and n["days"] == period_days - 14


async def test_zero_fte_nurse_excluded_from_headcount(session):
    """0 FTE(주<16h) 간호사는 명단엔 있으나 headcount 계상 제외(적대검토 LOW — 인원 부풀림)."""
    period_days = (END - START).days + 1
    full = _nurse("Z1")
    zero = _nurse("Z2")
    zero.weekly_hours = 10                      # <16h → part_time_fte 0.0
    session.add_all([full, zero])
    await session.flush()
    nd = await mon.nurse_days(session, start=START, end=END)
    by = {n["employee_no"]: n for n in nd["nurses"]}
    assert by["Z1"]["contributing"] is True and by["Z1"]["days"] == period_days
    assert by["Z2"]["contributing"] is False and by["Z2"]["days"] == 0
    assert nd["headcount"] == 1                 # 기여 인원만(0 FTE 제외)
    assert len(nd["nurses"]) == 2               # 명단은 전원


async def test_set_nurse_attributes_audited(session):
    """set_nurse_attributes 크로스도메인 쓰기 감사기록(적대검토 LOW)."""
    from erp.audit.models import AuditLog
    from erp.regulatory.institution_service import set_nurse_attributes
    from sqlalchemy import func, select as sel

    nurse = _nurse("A1")
    session.add(nurse)
    await session.flush()
    await set_nurse_attributes(session, actor=7, employee_id=nurse.id,
                               weekly_hours=24, nursing_exclude_reason="간호감독")
    n = await session.scalar(sel(func.count()).select_from(AuditLog).where(
        AuditLog.entity == "hr.employees", AuditLog.target_id == str(nurse.id)))
    assert n == 1
