"""간호등급 모니터링 (인사 P3) — 수동 계산기 → 자동 집계 + 충원 시뮬레이션.

자동화 범위:
- 간호사 재직일수(nurse_days): hr.employees(job_category=NURSE) 기간 중복일수 합,
  **휴직(LeaveAbsence) 연속 부재 16일+ 구간 제외**(고시 다.3 — 보유 데이터로 반영).
  단시간 환산(주 근로시간)은 근로시간 필드 도입 후 — 현재 전일제 1.0 가정(문서화).
- 재원일수(patient_days): 정식은 HIS 0시 재원 집계(M0a 대기) — 보조로
  billing_lines(INPATIENT) distinct(chart, 일자) **추정치** 제공, 수동 입력 우선.
- 시뮬레이션: 간호사 ±k명 시 등급 변화(상위 진입 필요 인원·1명 이탈 위험).
"""
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.hr.models import Employee, LeaveAbsence
from erp.patient.models import BillingLine
from erp.regulatory.nursing_grade import GradeResult, compute_grade, quarter_window

_ABSENCE_EXCLUDE_MIN_DAYS = 16   # 연속 부재 16일+ 제외 (고시)


def _overlap_days(a_start: date, a_end: date | None, b_start: date, b_end: date) -> int:
    s = max(a_start, b_start)
    e = min(a_end or b_end, b_end)
    return max(0, (e - s).days + 1)


class NursingMonitor:
    async def nurse_days(self, session: AsyncSession, *, start: date, end: date) -> dict:
        nurses = (await session.scalars(
            select(Employee).where(Employee.job_category == "NURSE"))).all()
        if not nurses:
            # 응답 형태는 분기와 무관하게 같게 — 간호 인력 0명 기관에서 화면이 excluded_from_count.length로
            # 크래시했다(2026-10-09 시나리오 S-G)
            return {"nurse_days": 0.0, "headcount": 0, "nurses": [], "excluded_from_count": []}
        absences = (await session.scalars(
            select(LeaveAbsence).where(
                LeaveAbsence.employee_id.in_([n.id for n in nurses])))).all()
        abs_by_emp: dict[int, list[LeaveAbsence]] = {}
        for a in absences:
            abs_by_emp.setdefault(a.employee_id, []).append(a)

        from erp.regulatory.nursing_grade import part_time_fte
        total, rows, dropped = 0.0, [], []
        for n in nurses:
            work = _overlap_days(n.hired_on, n.resigned_on, start, end)
            if work <= 0:
                continue
            # 산정 제외 대상(고시 — 간호감독·노조전임·호스피스 등)은 인원에서 제외
            if n.nursing_exclude_reason:
                dropped.append({"employee_no": n.employee_no, "name": n.name,
                                "reason": n.nursing_exclude_reason})
                continue
            excluded = 0
            for a in abs_by_emp.get(n.id, []):
                last = (a.return_on - timedelta(days=1)) if a.return_on else None
                # 임계 판정은 부재의 '실제 연속 길이'로 — 산정창 겹침일수(ov)로 판정하면 장기 휴직이
                # 분기 경계에 걸쳐 창에는 며칠만 겹칠 때 16일 미만으로 오판돼 재직 계상됨(적대검토 MED).
                # 복귀일 미정(return_on=None)은 무기한 휴직 → 임계 충족으로 간주(창 겹침분만 차감).
                full_len = (a.return_on - a.start_on).days if a.return_on else None
                ov = _overlap_days(a.start_on, last, start, end)
                if full_len is None or full_len >= _ABSENCE_EXCLUDE_MIN_DAYS:
                    excluded += ov
            work_days = max(0, work - excluded)
            # 단시간 환산(주 근로시간 → 0.4~0.9·NULL/≥40=1.0) — 고시 다.3)나)
            fte = part_time_fte(float(n.weekly_hours)) if n.weekly_hours is not None else 1.0
            days = work_days * fte
            total += days
            rows.append({"employee_id": n.id, "employee_no": n.employee_no, "name": n.name,
                         "days": round(days, 2), "excluded_absence_days": excluded,
                         "weekly_hours": float(n.weekly_hours) if n.weekly_hours is not None else None,
                         "fte": fte, "contributing": days > 0})
        # headcount = 실제 산정 기여 인원(재원일수>0) — 0 FTE(주<16h)·전월 부재자를 계상에서 제외
        # (적대검토 LOW — 표시 인원 부풀림). 명단(nurses)은 전원 유지·contributing 플래그로 구분.
        return {"nurse_days": total,
                "headcount": sum(1 for r in rows if r["contributing"]),
                "nurses": rows, "excluded_from_count": dropped}

    async def patient_days_estimate(self, session: AsyncSession, *,
                                    start: date, end: date) -> int:
        """입원 라인 distinct(차트,일자) — 추정치(정식 0시 재원 집계는 HIS 대기)."""
        rows = (await session.execute(
            select(BillingLine.chart_no, BillingLine.treated_on).distinct()
            .where(BillingLine.encounter_type == "INPATIENT",
                   BillingLine.cancelled.is_(False),
                   BillingLine.treated_on >= start,
                   BillingLine.treated_on <= end))).all()
        return len(rows)

    async def monitor(self, session: AsyncSession, *, hospital_type: str | None = None,
                      patient_days: float | None = None,
                      as_of: date | None = None) -> dict:
        from erp.regulatory.institution_service import (
            InstitutionProfileService, NursingCensusService,
        )
        # 종별 — 지정 없으면 기관 규제 프로필에서(미설정 시 HOSPITAL)
        if hospital_type is None:
            hospital_type = (await InstitutionProfileService().effective(session))["hospital_type"]
        start, end = quarter_window(as_of or date.today())
        nd = await self.nurse_days(session, start=start, end=end)
        est = await self.patient_days_estimate(session, start=start, end=end)
        # 재원일수 우선순위: 수동 파라미터 > 저장 census(관리자 실측) > HIS 0시 census > 입원 라인 추정
        saved = None if patient_days is not None else \
            await NursingCensusService().get_for(session, period_start=start)
        census_days = None
        if patient_days is None and saved is None:
            try:
                from erp.integrations.his_billing import fetch_census
                c = await fetch_census(start, end)
                if c["days"] > 0:
                    census_days = c["patient_days"]
            except Exception:  # noqa: BLE001 — census 미가용 시 추정 폴백
                census_days = None
        if patient_days is not None:
            pd_used, pd_source = patient_days, "MANUAL"
        elif saved is not None:
            pd_used, pd_source = float(saved.patient_days), "SAVED_CENSUS"
        elif census_days is not None:
            pd_used, pd_source = census_days, "HIS_CENSUS"
        else:
            pd_used, pd_source = float(est), "BILLING_LINES_ESTIMATE"
        result = compute_grade(patient_days=pd_used, nurse_days=nd["nurse_days"],
                               period_start=start, period_end=end,
                               hospital_type=hospital_type)
        days = (end - start).days + 1

        def _grade_with(extra_nurses: int) -> GradeResult:
            return compute_grade(patient_days=pd_used,
                                 nurse_days=nd["nurse_days"] + extra_nurses * days,
                                 period_start=start, period_end=end,
                                 hospital_type=hospital_type)

        # 상위 등급 진입 필요 인원(+30명 한도 탐색) · 1명 이탈 시 등급
        upgrade_needed = None
        if result.grade is not None and pd_used > 0:
            for k in range(1, 31):
                if _grade_with(k).grade != result.grade:
                    upgrade_needed = k
                    break
        downgrade_risk = (_grade_with(-1).grade
                          if nd["nurse_days"] >= days else None)

        return {"period": {"start": start, "end": end},
                "hospital_type": hospital_type,
                "patient_days": pd_used,
                "patient_days_source": pd_source,
                "patient_days_estimate": est,
                "nurse": nd,
                "avg_patients": result.avg_patients, "avg_nurses": result.avg_nurses,
                "ratio": result.ratio, "grade": result.grade,
                "simulation": {"upgrade_nurses_needed": upgrade_needed,
                               "grade_if_one_leaves": downgrade_risk},
                "주": "재원일수 정식 집계는 HIS 0시 재원 기준(M0a) — 현 추정치는 입원 라인 기반. "
                      "단시간 환산(0.4~0.9)은 주 근로시간 필드 도입 후."}
