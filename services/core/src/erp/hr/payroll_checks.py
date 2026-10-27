"""급여 법정 점검 — 최저임금 (v1.303.0·2026-10-11).

급여 생성·확정 어디에도 최저임금 검사가 없었다(코드에 최저임금 값 자체가 없었다). 막지 않고 **드러낸다** — 수습 감액(최대 10%·1년 이상
계약)·소정근로시간 단축 등 정당한 예외가 있어 차단은 사람 판단을 대신하게 된다. 미확정 급여의 미달은 결정·조치함에 뜬다.

산입범위(최저임금법 §6④·2024년부터 정기상여금·현금성 복리후생비 전액 산입):
- 산입: 기본급·정기상여금·고정 수당(자격·교대 정액·감시단속)·식대(현금성 복리후생) — 무급휴가 공제는 차감 그대로
- 제외: 연장·야간(시간 비례)·휴일근로·당직(소정근로 밖 대가)·성과급(매월 정기 지급 아님)
월 기준시간 = 209h(주 40h·주휴 포함) × 주 소정근로시간/40(단시간) × 재직 역일 비율(입·퇴사월). 일용은 일당 ÷ 8h.
"""
from __future__ import annotations

from erp.hr.payroll_rules import STANDARD_MONTHLY_HOURS

INCLUDED = frozenset({"BASE", "BONUS", "QUAL", "SHIFT", "WATCH", "MEAL", "UNPAID_LEAVE"})
# NIGHT는 카탈로그 이름이 '교대수당'이지만 급여 규칙이 야간시간 × 단가로 계산한다(야간근로 대가) → 제외
EXCLUDED = frozenset({"OT", "HOLIDAY", "NIGHT", "NIGHT_PREMIUM", "DUTY", "INCENTIVE"})


def check_min_wage(lines: list[dict], *, hourly_min: int | None, employment_type: str | None,
                   weekly_hours: float | None, employed_days: int, month_days: int) -> dict:
    """급여 줄 → 최저임금 비교. {ok: True/False, ...} 또는 판단 불가면 {skip: 사유}.

    - 무급휴가: 임금(UNPAID_LEAVE 차감)과 시간(일수 × 8h)을 **함께** 뺀다(최저임금법 §6 — 실제 제공 시간당 임금. 임금만 빼면 적법한 급여가
      미달로 나왔다·검토 MED).
    - 감시단속: 근기법 §63은 근로시간 규정 제외일 뿐 최저임금은 적용 — 소정근로시간이 주 40h를 넘는 게 보통(격일 24h)이라 209h로 가정하면
      실제 미달을 가린다 → 주 소정근로시간이 등록된 경우만 상한 없이 비교, 없으면 판단 보류(사유 표시).
    - 일용: 기록된 일당(BASE 계산근거) ÷ 8h — 근로시간이 8h가 아니면 결과가 다르다(주의 문구).
    - 분류되지 않은 지급 항목(관리자가 추가한 고정 수당 등)은 산입으로 보고 '미분류'로 드러낸다(빼면 거짓 미달).
    """
    if not hourly_min:
        return {"skip": "최저임금 미등록"}
    base = next((ln for ln in lines if ln["item_code"] == "BASE"), None)
    if employment_type == "DAILY":
        b = (base or {}).get("calc_basis") or {}
        daily = b.get("daily")
        if not daily:
            return {"skip": "일당 기록 없음"}
        per_hour = float(daily) / 8
        return {"ok": per_hour >= hourly_min, "kind": "일당(8h 가정)", "hourly": round(per_hour), "hourly_min": hourly_min,
                "included": float(daily), "threshold": hourly_min * 8,
                "shortfall": max(round(hourly_min * 8 - float(daily)), 0), "unclassified": []}
    earn = [ln for ln in lines if ln.get("item_kind", "EARNING") == "EARNING"]
    unclassified = sorted({ln["item_code"] for ln in earn if ln["item_code"] not in INCLUDED | EXCLUDED})
    included = sum(float(ln["amount"]) for ln in earn if ln["item_code"] not in EXCLUDED)
    if included <= 0:
        return {"skip": "산입 임금 없음"}
    if employment_type == "WATCH":
        if not weekly_hours:
            return {"skip": "감시단속 — 주 소정근로시간 미등록(209h 가정 불가)"}
        hours = STANDARD_MONTHLY_HOURS * float(weekly_hours) / 40.0
    else:
        hours = STANDARD_MONTHLY_HOURS * (min(float(weekly_hours), 40.0) / 40.0 if weekly_hours else 1.0)
    ratio = (employed_days / month_days) if month_days else 1.0
    unpaid = next((ln for ln in earn if ln["item_code"] == "UNPAID_LEAVE"), None)
    unpaid_days = float(((unpaid or {}).get("calc_basis") or {}).get("days") or 0)
    work_hours = max(hours * ratio - unpaid_days * 8, 0.0)
    if work_hours <= 0:
        return {"skip": "소정근로시간 0(전월 무급 등)"}
    threshold = hourly_min * work_hours
    return {"ok": included + 0.5 >= threshold, "kind": "월급", "hourly": round(included / work_hours),
            "hourly_min": hourly_min, "hours": round(work_hours, 1), "unpaid_days": unpaid_days,
            "included": round(included), "threshold": round(threshold),
            "shortfall": max(round(threshold - included), 0), "unclassified": unclassified}


async def month_report(session, *, yyyymm: str, limit: int = 300) -> dict:
    """그 달 급여의 최저임금 점검 — 미달 건(확정 여부 포함)·검사 수·기준값 출처. 읽기 전용."""
    import calendar
    from datetime import date

    from sqlalchemy import select

    from erp.common.months import require_ym
    from erp.hr import statutory
    from erp.hr.models import Employee, PayrollLine, PayrollRecord
    ym = require_ym(yyyymm)
    y, m = int(ym[:4]), int(ym[5:7])
    await statutory.refresh_db_params(session)
    hourly_min, src = statutory.min_wage_hourly(y)
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    recs = (await session.execute(select(PayrollRecord, Employee).join(Employee, Employee.id == PayrollRecord.employee_id)
                                  .where(PayrollRecord.yyyymm == ym).order_by(Employee.employee_no))).all()
    ids = [r.id for r, _ in recs]
    lines: dict[int, list[dict]] = {}
    for ln in (await session.scalars(select(PayrollLine).where(PayrollLine.record_id.in_(ids)))).all() if ids else []:
        lines.setdefault(ln.record_id, []).append({"item_code": ln.item_code, "item_kind": ln.item_kind,
                                                   "amount": float(ln.amount), "calc_basis": ln.calc_basis})
    rows, checked = [], 0
    skipped: dict[str, int] = {}
    unclassified: set[str] = set()
    for r, e in recs:
        start = max(first, e.hired_on) if e.hired_on else first
        end = min(last, e.resigned_on) if e.resigned_on else last
        employed = max((end - start).days + 1, 0)
        base_line = next((x for x in lines.get(r.id, []) if x["item_code"] == "BASE"), None)
        res = check_min_wage(lines.get(r.id, []), hourly_min=hourly_min, employment_type=e.employment_type,
                             weekly_hours=float(e.weekly_hours) if e.weekly_hours is not None else None,
                             employed_days=employed, month_days=last.day)
        if "skip" in res:
            skipped[res["skip"]] = skipped.get(res["skip"], 0) + 1
            continue
        checked += 1
        unclassified |= set(res.get("unclassified") or [])
        if not res["ok"] and len(rows) < limit:
            rows.append({"employee_no": e.employee_no, "name": e.name, "employment_type": e.employment_type,
                         "confirmed": r.confirmed_at is not None, "base": round(base_line["amount"]) if base_line else None,
                         **res})
    return {"yyyymm": ym, "hourly_min": hourly_min, "source": src, "checked": checked,
            "skipped": sum(skipped.values()), "skipped_by_reason": skipped, "unclassified_items": sorted(unclassified),
            "violations": rows, "included_items": sorted(INCLUDED), "excluded_items": sorted(EXCLUDED),
            "note": ("미달이어도 급여를 막지 않습니다. 예외: 수습 감액은 1년 이상 계약·수습 3개월 이내·최대 10%이고 단순노무(경비·청소 등)는 "
                     "감액 불가 · 정기상여금은 매월 지급분만 산입(분기·반기 일시 지급이면 그 달만 과대·다른 달 과소) · 일용은 하루 8h 가정.")}
