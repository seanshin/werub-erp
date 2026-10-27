"""일정 캘린더 (v1.302.0·2026-10-11) — 한 달에 언제 무엇이 돌아가고 언제 마감인지 한 화면에.

새로 저장하지 않는다 — 기존 소스를 그대로 읽어 모은다(정기업무 커널·컴플라이언스 만기·공휴일 판정·이의신청). 각 항목은 처리 화면 링크를
가진다. 권한: 화면 자체는 admin.recurring read(관리자·원장). 법정 기한은 regulatory.compliance, 이의신청은 claims.review **전체 범위**일
때만 넣는다(부서 범위 사용자에게 다른 부서 이의신청이 보이지 않게 — 목록 API와 같은 경계).
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

KINDS = {"recurring": "정기업무", "deadline": "법정 기한", "holiday": "공휴일", "appeal": "이의신청 기한"}
_FIXED_NAMES = {(1, 1): "신정", (3, 1): "3·1절", (5, 5): "어린이날", (6, 6): "현충일", (8, 15): "광복절",
                (10, 3): "개천절", (10, 9): "한글날", (12, 25): "성탄절"}


def _ev(d: date, kind: str, title: str, *, status: str | None = None, time: str | None = None,
        detail: str | None = None, href: str | None = None) -> dict:
    return {"date": d.isoformat(), "kind": kind, "title": title, "status": status, "time": time,
            "detail": detail, "href": href}


async def _recurring(session: AsyncSession, first: date, last: date, now: datetime) -> list[dict]:
    """켜진 정기업무(매일 제외)의 그 달 예정 + 실행 상태. 지연 = 대기 중이고 예정+SLA를 넘김(정기업무 화면과 같은 기준)."""
    from erp.common import recurring as rec
    from erp.common.kst import as_kst_wallclock
    from erp.common.models import RecurringTask, RecurringTaskRun
    tasks = (await session.scalars(select(RecurringTask).where(
        RecurringTask.enabled.is_(True), RecurringTask.cadence != "DAILY"))).all()
    out, wanted = [], []
    for t in tasks:
        seen: set[str] = set()
        d = first - timedelta(days=400)                    # 연간 업무(기간 끝+오프셋)까지 잡는 창
        while d <= last:
            k = rec.period_key(t.cadence, d)
            if k not in seen:
                seen.add(k)
                at = rec.scheduled_for(t.cadence, d, t.anchor)
                if first <= at.date() <= last:
                    wanted.append((t, k, at))
            d += timedelta(days=1)
    runs = {}
    if wanted:
        rows = (await session.scalars(select(RecurringTaskRun).where(
            RecurringTaskRun.task_code.in_({t.code for t, _, _ in wanted}),
            RecurringTaskRun.period_key.in_({k for _, k, _ in wanted})))).all()
        runs = {(r.task_code, r.period_key): r for r in rows}
    label = {"DONE": "완료", "FAILED": "실패", "SKIPPED": "생략", "WAITING_APPROVAL": "승인 대기", "RUNNING": "실행 중",
             "DUE": "대기"}
    for t, k, at in sorted(wanted, key=lambda x: x[2]):
        r = runs.get((t.code, k))
        created = as_kst_wallclock(t.created_at) if t.created_at is not None else None   # timestamptz 되읽기 = KST 벽시계(Phase0 규율)
        # 업무 등록 전·실행 기록이 남지 않는 먼 과거 기간은 '기록 없음'(확인 필요로 세지 않음)
        st = r.status if r is not None else ("예정" if at > now
                                              else "기록 없음" if (created and at < created) or at < now - timedelta(days=120)
                                              else "미생성")
        if r is not None and r.status == "DUE" and at + timedelta(days=t.sla_days or 0) < now:
            st = "OVERDUE"
        out.append(_ev(at.date(), "recurring", t.name, time=at.strftime("%H:%M"),
                       status=label.get(st, "지연" if st == "OVERDUE" else st),
                       detail=f"{t.code} · {k} · {t.execution_mode}", href="/admin/recurring-tasks"))
    return out


async def _deadlines(session: AsyncSession, first: date, last: date, today: date) -> list[dict]:
    from erp.app.compliance_service import ComplianceDeadlineService
    horizon = max(1, (last - today).days)        # 먼 달도 끝까지(365일 상한이 1년 뒤 달을 중간에서 잘랐다)
    data = await ComplianceDeadlineService().deadlines(session, as_of=today, horizon_days=horizon)
    band = {"long_overdue": "장기 경과", "overdue": "경과", "d7": "7일 이내", "d30": "30일 이내", "d90": "90일 이내"}
    out = []
    for it in data.get("items", []):
        due = it["due_on"] if isinstance(it["due_on"], date) else date.fromisoformat(str(it["due_on"]))
        if first <= due <= last:
            out.append(_ev(due, "deadline", f"{it['category']} — {it['title']}", status=band.get(it.get("band"), it.get("band")),
                           detail=" · ".join(x for x in (it.get("subject"), it.get("status")) if x), href="/compliance"))
    return out


async def _holidays(session: AsyncSession, first: date, last: date) -> list[dict]:
    from erp.claims import deadline
    from erp.common.models import Holiday
    db = {h.on_date: h for h in (await session.scalars(select(Holiday).where(
        Holiday.on_date >= first, Holiday.on_date <= last))).all()}
    out = []
    d = first
    while d <= last:
        if d in db:
            out.append(_ev(d, "holiday", db[d].name, status="캘린더 등록", href="/admin/org"))
        elif deadline.is_fixed_holiday(d):
            since = deadline.FIXED_HOLIDAYS_SINCE.get((d.month, d.day))
            out.append(_ev(d, "holiday", since[1] if since else _FIXED_NAMES.get((d.month, d.day), "고정 공휴일"),
                           status="고정 공휴일", href="/admin/org"))
        elif (d.month, d.day) in deadline.EXTRA_HOLIDAYS.get(d.year, set()):
            out.append(_ev(d, "holiday", "명절·대체공휴일", status="코드 표(캘린더 미등록)", href="/admin/org"))
        d += timedelta(days=1)
    return out


async def _appeals(session: AsyncSession, first: date, last: date, today: date) -> list[dict]:
    from erp.claims.models import Appeal
    rows = (await session.scalars(select(Appeal).where(
        Appeal.legal_deadline >= first, Appeal.legal_deadline <= last,
        Appeal.status.in_(("RECEIVED", "SUPPLEMENT"))).order_by(Appeal.legal_deadline))).all()
    return [_ev(a.legal_deadline, "appeal", f"이의신청 #{a.id} 법정 기한",
                status=f"D-{(a.legal_deadline - today).days}" if a.legal_deadline >= today else "기한 경과",
                detail=a.status, href="/claims") for a in rows]


async def month(session: AsyncSession, *, user, yyyymm: str, now: datetime) -> dict:
    """그 달 일정 — 소스별로 격리(하나가 실패해도 나머지는 보이고 실패는 errors로)."""
    from erp.common.months import require_ym
    from erp.common.policy import Role, ScopeKind, resolve_scope
    ym = require_ym(yyyymm)
    y, m = int(ym[:4]), int(ym[5:7])
    first, last = date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
    today = now.date()

    def allowed(resource: str) -> bool:
        return resolve_scope(resource, "read", role=Role(user.role), dept_id=user.dept_id,
                             user_id=user.user_id).kind == ScopeKind.ALL

    sources = [("recurring", lambda: _recurring(session, first, last, now)),
               ("holiday", lambda: _holidays(session, first, last))]
    hidden = []
    if allowed("regulatory.compliance"):
        sources.append(("deadline", lambda: _deadlines(session, first, last, today)))
    else:
        hidden.append("deadline")
    if allowed("claims.review"):
        sources.append(("appeal", lambda: _appeals(session, first, last, today)))
    else:
        hidden.append("appeal")
    events, errors = [], []
    for kind, fn in sources:
        try:
            async with session.begin_nested():
                events.extend(await fn())
        except Exception as exc:  # noqa: BLE001 — 소스 실패를 화면에 드러낸다
            errors.append({"kind": kind, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    events.sort(key=lambda e: (e["date"], e["time"] or "", e["kind"]))
    return {"yyyymm": ym, "first": first.isoformat(), "last": last.isoformat(), "today": today.isoformat(),
            "kinds": KINDS, "hidden_kinds": hidden, "events": events, "errors": errors}
