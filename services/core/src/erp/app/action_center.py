"""결정·조치함 — 사람이 정하거나 화면에서 처리할 일을 분류별로 한곳에 (2026-10-11).

결정 대기함(common.pending_decisions)은 '무엇을 할지' 정하는 항목만 담는다. 그런데 사람 몫의 일 상당수는 결정이 아니라
**다른 화면에서의 조치**다(간이세액표 원표 대조 후 활성화·공휴일 등록·인박스 DEAD 판단·담당 역할 배정). 그런 일은 각 화면에
흩어져 있어 들어가 보지 않으면 보이지 않았다 — 릴리즈 보고서·문서 목록에만 적혀 있었다.

여기서는 그 조치들을 **현재 데이터로 매번 계산**해(저장하지 않음 — 처리하면 저절로 사라진다) 결정 항목과 같은 분류 체계로 묶는다.
⚠ 영구 발화 금지: 각 조치는 처리하면 조건이 해소되는 것만 넣는다. 점검 하나가 실패해도 나머지는 보여주고, 실패는 숨기지 않는다.
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

# 분류 단일 소스 — 순서=화면 표시 순서. 결정 항목 등록·분류 변경은 이 키만 받는다.
CATEGORIES: list[tuple[str, str]] = [
    ("HR", "인사·급여"),
    ("FINANCE", "재무·회계"),
    ("INTEGRATION", "연동"),
    ("SECURITY", "보안"),
    ("PRIVACY", "개인정보"),
    ("POLICY", "정책"),
    ("OPS", "운영"),
    ("INFRA", "인프라"),
    ("ARCH", "아키텍처"),
    ("GENERAL", "일반"),
]
CATEGORY_KEYS = frozenset(k for k, _ in CATEGORIES)



def _item(key: str, category: str, title: str, detail: str, screen: str, screen_label: str,
          count: int | None = None) -> dict:
    # resolver_role: 연결 화면의 쓰기 권한(hr.compensation·admin.org·admin.staging·admin.recurring)이 모두 관리자 전용 —
    # 원장(DIRECTOR)도 이 목록은 보지만 처리는 관리자 몫임을 화면에 표시한다
    return {"key": key, "category": category, "title": title, "detail": detail,
            "screen": screen, "screen_label": screen_label, "count": count, "resolver_role": "ADMIN"}


async def _withholding_candidate(session: AsyncSession, today: date) -> list[dict]:
    from erp.hr.models import WithholdingTable
    cands = (await session.scalars(select(WithholdingTable).where(
        WithholdingTable.status == "CANDIDATE").order_by(WithholdingTable.effective_from))).all()
    actives = (await session.scalars(select(WithholdingTable).where(WithholdingTable.status == "ACTIVE"))).all()
    out = []
    for t in cands:
        # 같은 시행일 이후의 활성 표가 있으면(시행 전 미래 표 포함) 남은 후보는 대조 대상이 아니다
        if any(a.effective_from >= t.effective_from for a in actives):
            continue
        out.append(_item(
            f"withholding-table-{t.effective_from}", "HR",
            f"근로소득 간이세액표 {t.effective_from} 시행분 — 원표 대조 후 활성화",
            "후보로만 들어 있어 월 소득세는 아직 근사 산식으로 계산됩니다. 표본 칸을 홈택스 원표와 대조하고 근거를 적어 "
            "활성화하세요(활성화 전에 만든 미확정 급여는 같은 카드의 '세액 재계산'으로 맞춥니다). 원표와 다르면 사유를 적어 '후보 폐기'하세요.",
            "/admin/payroll-settings", "급여 설정 「근로소득 간이세액표」"))
    if not actives and not cands:
        # 마지막 후보를 폐기하면 후보 항목이 사라지지만 계산은 여전히 근사 산식이다 — 해소=표 활성화(다음 릴리즈로 표 적재)
        out.append(_item(
            "withholding-table-none", "HR", "활성 간이세액표 없음 — 월 소득세를 근사 산식으로 계산 중",
            "근사 산식은 간이세액표보다 소득세를 더 뗍니다. 올바른 표는 릴리즈로 후보가 "
            "들어오면 대조 후 활성화하세요 — 개발 담당에게 표 적재를 요청하세요.",
            "/admin/payroll-settings", "급여 설정 「근로소득 간이세액표」"))
    from erp.hr.withholding_table import pending_retax_count
    for t in actives:
        n, m = await pending_retax_count(session, table_id=t.id)
        if n or m:
            title = (f"미확정 급여 {n}건 소득세가 {t.effective_from} 시행 간이세액표 기준이 아님" if n
                     else f"미확정 급여 {m}건 소득세 개별 확인 필요({t.effective_from} 시행 표)")
            out.append(_item(
                f"withholding-retax-{t.id}", "HR", title,
                "표를 활성화하기 전(또는 다른 표로) 만든 급여입니다. 미리보기로 직원별 차액을 보고 소득세·지방소득세 줄만 재계산하세요"
                "(지급·다른 공제 줄은 그대로)." + (f" 성과급 합산 등 {m}건은 미리보기의 제외 사유대로 개별 처리(확정하면 사라짐)." if m else ""),
                "/admin/payroll-settings", "급여 설정 「근로소득 간이세액표」", count=n + m))
    return out


async def _holidays(session: AsyncSession, today: date) -> list[dict]:
    """공휴일 — 계산은 DB 캘린더 ∪ 코드 표(`claims.deadline.EXTRA_HOLIDAYS`)라 둘 다 비었을 때만 '미등록'.

    해마다 바뀌는 날(음력·대체·선거일·2026 신설 노동절·제헌절)은 `common.holiday_candidates` 후보로 두고, 아직 등록 안 된 후보가 있고
    그 해 검토 기록이 없으면 묻는다 — 검토(등록·뺀 날짜·근거)하면 닫힌다(이견 날짜를 빼도 영구 발화하지 않음).
    """
    from erp.claims.deadline import EXTRA_HOLIDAYS
    from erp.common import holiday_candidates as hc
    from erp.common.models import Holiday, HolidayCandidateReview
    out = []
    rows = (await session.execute(select(Holiday.on_date, Holiday.kind, Holiday.name).where(
        Holiday.on_date >= date(today.year, 1, 1), Holiday.on_date <= date(today.year + 1, 12, 31)))).all()
    dates = {d for d, _, _ in rows}
    # 음력 명절 등록 여부 — 종류 '음력명절' 또는 명칭(설·추석). 수기 등록 기본 종류가 '공휴일'이라 종류만 보면 등록해도 계속 뜬다
    lunar_years = {d.year for d, k, n in rows if k == "LUNAR" or "설" in n or "추석" in n}
    reviewed = set((await session.scalars(select(HolidayCandidateReview.year).where(
        HolidayCandidateReview.year.in_([today.year, today.year + 1])))).all())
    for y in [today.year] + ([today.year + 1] if today.month >= 11 else []):
        missing = [c for c in hc.for_year(y) if c["on_date"] not in dates]
        if missing and y not in reviewed:
            names = ", ".join(f"{c['on_date'].strftime('%m/%d')} {c['name']}" for c in missing[:4])
            out.append(_item(f"holiday-candidates-{y}", "HR", f"{y}년 공휴일 후보 {len(missing)}건 검토·등록",
                             f"미등록 후보: {names}{' 외' if len(missing) > 4 else ''}. 원문(월력요항·관공서의 공휴일에 관한 규정)과 "
                             "대조해 등록할 날짜를 고르세요 — 빠지면 그날 근무의 휴일근로 가산·청구 공휴가산·이의신청 기한이 평일로 계산됩니다.",
                             "/admin/org", "기관 설정 「공휴일 캘린더」", count=len(missing)))
        elif not (EXTRA_HOLIDAYS.get(y) or y in lunar_years):
            # 설·추석(음력)은 해마다 있다 — 음력 공휴일이 코드에도 DB에도 없으면 미등록(선거일 하나만 등록해도 닫히지 않게)
            out.append(_item(f"holidays-{y}", "HR", f"{y}년 공휴일(설·추석·대체공휴일) 미등록",
                             "음력 명절·대체공휴일이 없습니다 — 월력요항을 보고 캘린더에 등록하세요(종류 '음력명절'·후보 목록은 개발 담당에게 요청).",
                             "/admin/org", "기관 설정 「공휴일 캘린더」"))
    return out


async def _statutory_years(session: AsyncSession, today: date) -> list[dict]:
    """올해분은 언제나(미등록이면 지금 급여·원천징수가 멈춘 상태), 이듬해분은 12월부터 묻는다.

    ⚠ 이듬해분만 보면 1월 1일에 항목이 사라진다 — 문제가 가장 심각해지는 순간에 조용해진다(2026-10-11 검토).
    """
    from erp.hr.statutory import refresh_db_params, registered_years
    await refresh_db_params(session)
    reg = registered_years()
    years = [today.year] + ([today.year + 1] if today.month == 12 else [])
    out = []
    for y in years:
        missing = [label for k, label in (("insurance", "4대보험 요율"), ("tax", "소득세 파라미터")) if y not in reg[k]]
        if not missing:
            continue
        detail = ("지금 급여 생성·원천징수 계산이 멈춰 있습니다 — 고시 대조 후 등록하세요(개정 없으면 전년 승계)."
                  if y == today.year else
                  "미등록 상태로 해를 넘기면 1월 급여 생성·원천징수 계산이 멈춥니다(고시 대조 · 개정 없으면 전년 승계).")
        out.append(_item(f"statutory-{y}", "HR", f"{y}년 {'·'.join(missing)} 등록", detail,
                         "/admin/payroll-settings", "급여 설정 「법정 요율」"))
    return out


async def _inbound_dead(session: AsyncSession, today: date) -> list[dict]:
    from erp.integrations.models import HisInboundEvent
    n = int(await session.scalar(select(func.count()).select_from(HisInboundEvent).where(
        HisInboundEvent.status == "DEAD")) or 0)
    if not n:
        return []
    return [_item("inbound-dead", "INTEGRATION", f"연동 수신 이벤트 처리 중단 {n}건",
                  "재시도가 끝나 자동으로는 반영되지 않습니다 — 건별 사유를 보고 재처리·조치 후 반영·종결 중 고르세요.",
                  "/admin/inbound", "연동 수신함", count=n)]


async def _owner_unstaffed(session: AsyncSession, today: date) -> list[dict]:
    from erp.common.models import RecurringTask, User
    roles = set((await session.scalars(select(RecurringTask.owner_role).where(
        RecurringTask.enabled.is_(True), RecurringTask.owner_role.isnot(None)))).all())
    if not roles:
        return []
    staffed = set((await session.scalars(select(User.role).where(
        User.role.in_(roles), User.is_active.is_(True)).distinct())).all())
    empty = sorted(roles - staffed)
    if not empty:
        return []
    return [_item("recurring-owner-unstaffed", "OPS", f"정기업무 담당 역할에 사용자 없음 — {', '.join(empty)}",
                  "알림·리마인드가 관리자 폴백으로만 갑니다. 역할을 배정하거나 업무 담당을 바꾸세요.",
                  "/admin/recurring-tasks", "정기업무 관리", count=len(empty))]


async def _batch_jobs(session: AsyncSession, today: date) -> list[dict]:
    """배치 작업 — 일시정지 중·마지막 실행 오류·기대 주기 3배 넘게 안 돈 잡(재개·정상 실행하면 사라짐).

    Redis에 닿지 않으면(로컬·장애) 점검 실패로 드러난다(빈 목록으로 숨기지 않음). 일정이 아직 게시되지 않았으면(구버전 실행기)
    판정하지 않는다.
    """
    import time

    from erp.common import batch_jobs
    from erp.common.redis_client import get_redis
    ov = await batch_jobs.overview(get_redis())
    if not ov["schedule_published"]:
        return []
    now = int(time.time())
    paused = [j for j in ov["jobs"] if j["registered"] and j["paused"]]
    failed = [j for j in ov["jobs"] if j["registered"] and not j["paused"] and j["last_status"] in ("error", "missed")]
    # 기대 주기 = 30초마다 다시 게시되는 일정의 interval_s(종전 레지스트리 키는 기동 시 1회·7일 TTL이라 일주일 뒤 판정이 꺼졌다)
    late = [j for j in ov["jobs"] if j["registered"] and not j["paused"] and j.get("interval_s") and j["last_run"]
            and now - j["last_run"] > j["interval_s"] * 3]
    mismatch = [j for j in ov["jobs"] if j.get("pause_mismatch")]
    out = []
    if mismatch:
        out.append(_item("batch-pause-mismatch", "OPS",
                         f"배치 작업 {len(mismatch)}개 일시정지 기록과 실제 상태가 다름 — " + ", ".join(j["label"] for j in mismatch),
                         "실행기가 30초 안에 기록대로 맞춥니다. 계속 보이면 실행기(worker-batch)를 확인하세요.",
                         "/admin/batch-jobs", "배치 작업", count=len(mismatch)))
    if paused:
        out.append(_item("batch-paused", "OPS", f"배치 작업 {len(paused)}개 일시정지 중 — " + ", ".join(j["label"] for j in paused),
                         "멈춘 동안 해당 자동 처리가 돌지 않습니다. 점검이 끝났으면 재개하세요(사유·요청자는 배치 작업 화면).",
                         "/admin/batch-jobs", "배치 작업", count=len(paused)))
    if failed:
        out.append(_item("batch-failed", "OPS", f"배치 작업 {len(failed)}개 마지막 실행 실패 — " + ", ".join(j["label"] for j in failed),
                         "실패 사유를 보고 원인을 고친 뒤 '지금 실행'으로 확인하세요(다음 정상 실행이면 사라짐).",
                         "/admin/batch-jobs", "배치 작업", count=len(failed)))
    if late:
        out.append(_item("batch-late", "OPS", f"배치 작업 {len(late)}개가 예정보다 오래 안 돌았음 — " + ", ".join(j["label"] for j in late),
                         "기대 주기의 3배 넘게 실행 기록이 없습니다 — 실행기(worker-batch) 상태를 확인하세요.",
                         "/admin/batch-jobs", "배치 작업", count=len(late)))
    return out


async def _min_wage(session: AsyncSession, today: date) -> list[dict]:
    """최저임금 미달 — 이번 달·지난달 **미확정** 급여만(확정분은 정정 절차가 따로라 영구 발화 방지). 고치거나 확정하면 사라진다."""
    from erp.hr.payroll_checks import month_report
    out = []
    for d in (today.replace(day=1) - timedelta(days=1), today):
        ym = f"{d.year}-{d.month:02d}"
        rep = await month_report(session, yyyymm=ym)
        bad = [v for v in rep["violations"] if not v["confirmed"]]
        if bad:
            out.append(_item(f"min-wage-{ym}", "HR", f"{ym} 미확정 급여 {len(bad)}건 최저임금 미달",
                             f"기준 시급 {rep['hourly_min']:,}원({rep['source']}) — 최대 부족 {max(v['shortfall'] for v in bad):,}원. "
                             "수습 감액·단시간 등 예외가 아니면 확정 전에 기본급·근로시간을 바로잡으세요.",
                             "/hr/statutory", "4대보험·연말정산 「최저임금 점검」", count=len(bad)))
    return out


_PROBES = [
    ("간이세액표", _withholding_candidate),
    ("공휴일", _holidays),
    ("법정 요율", _statutory_years),
    ("연동 수신함", _inbound_dead),
    ("정기업무 담당", _owner_unstaffed),
    ("배치 작업", _batch_jobs),
    ("최저임금", _min_wage),
]


async def screen_actions(session: AsyncSession, *, today: date) -> dict:
    """화면 조치 목록 — 점검마다 savepoint로 격리(하나가 실패해도 나머지는 보이고, 실패는 errors로 드러난다)."""
    items: list[dict] = []
    errors: list[dict] = []
    for name, probe in _PROBES:
        try:
            async with session.begin_nested():
                items.extend(await probe(session, today))
        except Exception as exc:  # noqa: BLE001 — 점검 실패를 화면에 드러낸다(삼키지 않음)
            log.warning("action-center probe failed: %s", name, exc_info=True)
            errors.append({"probe": name, "error": f"{type(exc).__name__}: {str(exc)[:120]}"})
    return {"categories": [{"key": k, "label": label} for k, label in CATEGORIES],
            "actions": items, "errors": errors}
