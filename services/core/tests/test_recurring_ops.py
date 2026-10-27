"""정기업무(주/월/분기) 러너 — 주기엔진·claim-then-run·물질화·멱등·실패격리·섀도우·수동실행.

기획서 M1: docs/01-plan/erp-recurring-operations-plan-2026-07-25.md.
"""
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import func, select

from erp.app.recurring_service import RecurringRunner
from erp.common import recurring as rec
from erp.common.models import RecurringTask, RecurringTaskRun
from erp.errors import ValidationError

runner = RecurringRunner()
_calls: dict[str, int] = {"ok": 0, "fail": 0}


@rec.register_handler("test.auto_ok")
async def _h_ok(session, period_key, ctx):
    _calls["ok"] += 1
    return {"processed": 3, "period": period_key}


@rec.register_handler("test.auto_fail")
async def _h_fail(session, period_key, ctx):
    _calls["fail"] += 1
    raise RuntimeError("boom " + "x" * 300)     # 긴 메시지 → [:120] 절단 확인


@rec.register_handler("test.ai_draft")
async def _h_draft(session, period_key, ctx):
    return {"summary": f"draft {period_key}", "question": "초안 승인?",
            "options": [{"key": "approve", "label": "승인"}, {"key": "reject", "label": "반려"}]}


@pytest.fixture(autouse=True)
def _reset_calls():
    _calls["ok"] = _calls["fail"] = 0


async def _task(session, **kw):
    # created_at 고정(테스트 고정 now들과 같은 기간의 시작) — 실제 시계를 쓰면 고정
    # now(예: 2026-07-15) 테스트가 달력이 그 기간을 지나는 순간 F3 게이팅(생성 전 종료
    # 기간 백필 안 함)에 걸려 깨진다(2026-08-01부터 실제 발생). 더 이르게 잡으면 반대로
    # 직전 기간(2026-06)이 게이트를 통과해 catchup 기대값이 달라진다.
    defaults = dict(code="t", name="테스트업무", domain="common", cadence="MONTHLY",
                    execution_mode="AUTO", handler_ref="test.auto_ok", anchor={},
                    created_at=datetime(2026, 7, 1))
    defaults.update(kw)
    t = RecurringTask(**defaults)
    session.add(t)
    await session.flush()
    return t


# ---- 주기 엔진(순수 함수) ----

def test_period_key_all_cadences():
    d = date(2026, 7, 15)
    assert rec.period_key("DAILY", d) == "2026-07-15"
    assert rec.period_key("MONTHLY", d) == "2026-07"
    assert rec.period_key("QUARTERLY", d) == "2026-Q3"
    assert rec.period_key("YEARLY", d) == "2026"
    assert rec.period_key("WEEKLY", date(2026, 1, 1)) == "2026-W01"     # ISO 주


def test_quarter_bounds():
    assert rec.period_bounds("QUARTERLY", date(2026, 8, 3)) == (date(2026, 7, 1), date(2026, 9, 30))
    assert rec.quarter_of(1) == 1 and rec.quarter_of(12) == 4


def test_scheduled_for_period_end_offset_vat():
    """부가세 분기확정 = 분기 종료 후 익월 25일 09:00 (basis=PERIOD_END·offset=25)."""
    anchor = {"basis": "PERIOD_END", "offset_days": 25, "hour": 9}
    # Q3 종료 2026-09-30 + 25일 = 2026-10-25 09:00
    assert rec.scheduled_for("QUARTERLY", date(2026, 8, 3), anchor) == datetime(2026, 10, 25, 9, 0)


# ---- 러너 ----

async def test_run_due_executes_auto(session):
    await _task(session)
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now)
    assert (s["materialized"], s["executed"], s["done"], s["failed"]) == (1, 1, 1, 0)
    assert _calls["ok"] == 1
    run = await session.scalar(select(RecurringTaskRun).where(RecurringTaskRun.task_code == "t"))
    assert run.status == "DONE" and run.period_key == "2026-07"
    assert run.result["processed"] == 3 and run.triggered_by == "SCHEDULER"
    assert run.duration_ms is not None


async def test_idempotent_second_run_noop(session):
    await _task(session)
    now = datetime(2026, 7, 15, 12, 0)
    await runner.run_due(session, now=now)
    s2 = await runner.run_due(session, now=now)          # 같은 기간 재실행
    assert s2["executed"] == 0 and _calls["ok"] == 1     # 핸들러 1회만


async def test_not_yet_due_skipped(session):
    # 예정 시각 전(월 시작 이전은 불가하니 익월 basis로) — 다음달 1일 예정
    await _task(session, cadence="MONTHLY", anchor={"basis": "PERIOD_START", "hour": 23})
    now = datetime(2026, 7, 15, 12, 0)
    # 07-01 23:00 <= now(07-15) 이므로 due. 대신 아직 안 온 케이스는 due_list로 검증
    await _task(session, code="future", cadence="MONTHLY",
                anchor={"basis": "PERIOD_END", "offset_days": 5})   # 08-05 예정
    rows = {r["code"]: r for r in await runner.due_list(session, now=now)}
    assert rows["future"]["status"] == "SCHEDULED"      # 아직 예정 전 → 물질화/실행 안 됨


async def test_failure_isolated_and_truncated(session):
    await _task(session, code="t", handler_ref="test.auto_fail")
    await _task(session, code="ok2", handler_ref="test.auto_ok")
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now)
    assert s["done"] == 1 and s["failed"] == 1          # 실패가 성공을 막지 않음
    fail = await session.scalar(select(RecurringTaskRun).where(RecurringTaskRun.task_code == "t"))
    assert fail.status == "FAILED"
    assert fail.error is not None and len(fail.error) <= 120     # [:120] 절단


async def test_catchup_runs_prior_period(session):
    """catchup=1 — 직전 기간 소급 실행(워커가 기간 통째로 놓친 경우 복구)."""
    t = await _task(session, code="t", cadence="MONTHLY")
    t.created_at = datetime(2026, 5, 1)          # 오래 전 생성 → 소급 게이팅 통과
    await session.flush()
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now, catchup=1)
    assert s["executed"] == 2 and _calls["ok"] == 2      # 6월 + 7월
    pkeys = {r.period_key for r in (await session.scalars(select(RecurringTaskRun))).all()}
    assert pkeys == {"2026-06", "2026-07"}


async def test_catchup_gated_by_created_at(session):
    """생성 이전에 종료된 기간은 백필하지 않음(F3 게이팅) — 6월은 생략, 7월만."""
    t = await _task(session, code="t", cadence="MONTHLY")
    t.created_at = datetime(2026, 7, 10)         # 7월 중 생성 → 6월(종료 6/30<7/10)은 게이트
    await session.flush()
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now, catchup=1)
    assert s["executed"] == 1 and _calls["ok"] == 1
    pkeys = {r.period_key for r in (await session.scalars(select(RecurringTaskRun))).all()}
    assert pkeys == {"2026-07"}                   # 6월 미백필


async def test_manual_and_approval_not_auto_run(session):
    await _task(session, code="man", execution_mode="MANUAL", handler_ref=None)
    await _task(session, code="appr", execution_mode="AUTO", requires_approval=True)
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now)
    assert s["executed"] == 0                            # 자동실행 대상 없음
    # 물질화는 되어 DUE로 남음(화면·알림·overdue 대상)
    runs = {r.task_code: r for r in (await session.scalars(select(RecurringTaskRun))).all()}
    assert runs["man"].status == "DUE" and runs["appr"].status == "DUE"


async def test_shadow_dry_run_materializes_only(session):
    await _task(session)
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now, dry_run=True)
    assert s["materialized"] == 1 and s["executed"] == 0 and _calls["ok"] == 0
    run = await session.scalar(select(RecurringTaskRun).where(RecurringTaskRun.task_code == "t"))
    assert run.status == "DUE"                           # 실행 안 함(섀도우)


async def test_due_list_flags(session):
    await _task(session, code="a", execution_mode="AUTO")
    await _task(session, code="m", execution_mode="MANUAL", handler_ref=None, sla_days=1,
               cadence="MONTHLY")
    now = datetime(2026, 7, 15, 12, 0)
    rows = {r["code"]: r for r in await runner.due_list(session, now=now)}
    assert rows["a"]["ai_runnable"] is True
    assert rows["m"]["ai_runnable"] is False
    assert rows["m"]["overdue"] is True                  # 07-01 + 1일 < 07-15


async def test_run_one_manual_and_rerun(session):
    await _task(session, code="t", handler_ref="test.auto_fail")
    # 최초 실행 → FAILED
    await runner.run_due(session, now=rec.now_naive())
    # 핸들러를 성공본으로 교체 후 force 재실행
    rec._HANDLERS["test.auto_fail"] = _h_ok
    try:
        run = await runner.run_one(session, code="t", actor=7, force=True)
        assert run.status == "DONE" and run.triggered_by == "ADMIN_UI" and run.actor == 7
    finally:
        rec._HANDLERS["test.auto_fail"] = _h_fail


async def test_run_one_rejects_approval_required(session):
    await _task(session, code="appr", requires_approval=True)
    with pytest.raises(ValidationError):
        await runner.run_one(session, code="appr", actor=1)
    with pytest.raises(ValidationError):
        await runner.run_one(session, code="nope", actor=1)     # 미존재


async def test_complete_manual(session):
    await _task(session, code="count", execution_mode="MANUAL", handler_ref=None, cadence="QUARTERLY")
    run = await runner.complete_manual(session, code="count", actor=9, note="분기 재고실사 완료")
    assert run.status == "DONE" and run.actor == 9 and run.result["manual"] is True
    # AUTO는 완료처리 거부
    await _task(session, code="auto", execution_mode="AUTO")
    with pytest.raises(ValidationError):
        await runner.complete_manual(session, code="auto", actor=9)


async def test_upsert_and_list(session):
    await runner.upsert_task(session, code="x", actor=1, name="신규", domain="finance",
                             cadence="MONTHLY", execution_mode="MANUAL")
    await runner.upsert_task(session, code="x", actor=1, enabled=False)   # 수정(멱등)
    tasks = {t["code"]: t for t in await runner.list_tasks(session)}
    assert tasks["x"]["enabled"] is False and tasks["x"]["name"] == "신규"
    with pytest.raises(ValidationError):
        await runner.upsert_task(session, code="y", actor=1, cadence="BOGUS")


# ---- HTTP 계층(RBAC) ----

async def test_http_due_and_run(session, client):
    from tests._http import auth_as
    await _task(session, code="t")
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/recurring-tasks/due", headers=headers)
    assert r.status_code == 200 and any(x["code"] == "t" for x in r.json()["due"])
    r = await client.post("/api/v1/admin/recurring-tasks/t/run", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "DONE"
    # STAFF는 read/write 모두 차단(인증은 통과·인가 403)
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/admin/recurring-tasks/due", headers=staff)).status_code == 403
    assert (await client.post("/api/v1/admin/recurring-tasks/t/run",
                              headers=staff)).status_code == 403


# ---- R11 관측성: health_signal + R7 인프라잡 패널 EP ----

def test_as_kst_wallclock_converts_true_utc():
    """asyncpg 되읽기=진짜 UTC aware(v1.257.1 prod 실측) — 라벨-strip(-9h 왜곡)이 아니라 KST 환산.

    구 as_naive_wallclock을 되읽기 값에 쓰면 방금 시작한 RUNNING이 stuck(+9h)으로 오탐되고
    일간 잡의 engine_stale(26h)도 매일 새벽 오탐(24h+9h=33h)된다."""
    from datetime import timezone

    from erp.common.kst import KST, as_kst_wallclock, as_naive_wallclock
    utc = datetime(2026, 7, 27, 23, 7, 36, tzinfo=timezone.utc)   # = KST 07-28 08:07:36
    assert as_kst_wallclock(utc) == datetime(2026, 7, 28, 8, 7, 36)
    assert as_naive_wallclock(utc) == datetime(2026, 7, 27, 23, 7, 36)  # 구 함수=왜곡(용도 한정)
    naive = datetime(2026, 7, 28, 8, 0)
    assert as_kst_wallclock(naive) is naive                       # sqlite(naive) 무변경
    assert as_kst_wallclock(datetime(2026, 7, 28, 8, 0, tzinfo=KST)) == naive


async def test_health_signal_ok_and_failed_recent(session):
    """FAILED 최근 48h → 신호 발화·48h 경과 → 자동 해제. 정상 DONE → ok."""
    await _task(session, code="hs", cadence="DAILY")
    now = datetime(2026, 7, 15, 12, 0)
    session.add(RecurringTaskRun(task_code="hs", period_key="2026-07-15",
                                 scheduled_for=datetime(2026, 7, 15, 5, 0), status="FAILED",
                                 finished_at=now))
    await session.flush()
    sig = await runner.health_signal(session, now=now)
    assert sig["ok"] is False and sig["failed_recent"] == ["hs"]
    sig = await runner.health_signal(session, now=datetime(2026, 7, 18, 12, 0))  # 48h 경과
    assert sig["failed_recent"] == []


async def test_health_signal_stuck_due_auto_only(session):
    """정체 DUE — AUTO만 신호(MANUAL 지연은 리마인드 소관)·RUNNING 고아 감지."""
    now = datetime(2026, 7, 15, 12, 0)
    await _task(session, code="a1", cadence="DAILY", execution_mode="AUTO")
    await _task(session, code="m1", cadence="DAILY", execution_mode="MANUAL", handler_ref=None)
    session.add_all([
        RecurringTaskRun(task_code="a1", period_key="2026-07-15",
                         scheduled_for=datetime(2026, 7, 15, 5, 0), status="DUE"),
        RecurringTaskRun(task_code="m1", period_key="2026-07-15",
                         scheduled_for=datetime(2026, 7, 15, 5, 0), status="DUE"),
        RecurringTaskRun(task_code="a1", period_key="2026-07-14",
                         scheduled_for=datetime(2026, 7, 14, 5, 0), status="RUNNING",
                         started_at=datetime(2026, 7, 14, 5, 20),
                         finished_at=datetime(2026, 7, 15, 11, 0)),   # engine_stale 억제용
    ])
    await session.flush()
    sig = await runner.health_signal(session, now=now)
    assert sig["stuck_due"] == ["a1"]                  # MANUAL 제외
    assert sig["stuck_running"] == ["a1"]              # 고아 RUNNING(2h+)
    assert sig["ok"] is False


async def test_health_signal_engine_stale(session):
    """enabled AUTO 존재+26h 무실행 → engine_stale. 신규 시드(26h 미경과·이력無)는 유예."""
    t = await _task(session, code="st", cadence="DAILY", execution_mode="AUTO",
                    created_at=rec.now_naive())        # 유예 기준점 = 실제 지금
    await session.flush()
    now = rec.now_naive() + timedelta(hours=30)        # created_at(지금) 기준 30h 후
    sig = await runner.health_signal(session, now=now)
    assert sig["engine_stale"] is True and sig["ok"] is False
    sig = await runner.health_signal(session, now=rec.now_naive() + timedelta(hours=1))
    assert sig["engine_stale"] is False                # 시드 직후 유예
    session.add(RecurringTaskRun(task_code="st", period_key="p", scheduled_for=now,
                                 status="DONE", finished_at=now - timedelta(hours=1)))
    await session.flush()
    sig = await runner.health_signal(session, now=now)
    assert sig["engine_stale"] is False and sig["ok"] is True
    assert t.enabled is True


async def test_http_infra_jobs_panel(session, client):
    """R7 EP — Redis 불가 환경에서도 200(잡 목록 空·신호는 DB 산출). STAFF 403."""
    from tests._http import auth_as
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/recurring-tasks/infra-jobs", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert "jobs" in body and "signal" in body and body["signal"]["ok"] is True
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/admin/recurring-tasks/infra-jobs",
                             headers=staff)).status_code == 403


def test_scheduler_job_event_listener_status_mapping():
    """R7 리스너 — executed/ok·error·missed 상태 매핑(루프 밖에선 기록 생략·무예외)."""
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
    from erp.workers import scheduler as sch

    class _Ev:
        def __init__(self, code, exc=None):
            self.code, self.exception, self.job_id = code, exc, "j1"
    # 루프 밖 — RuntimeError 삼킴(관측 생략)만 확인
    sch._on_job_event(_Ev(EVENT_JOB_EXECUTED))
    sch._on_job_event(_Ev(EVENT_JOB_ERROR, exc=RuntimeError("x")))
    sch._on_job_event(_Ev(EVENT_JOB_MISSED))


# ---- 주간 운영 브리핑(M4 백로그) ----

async def test_weekly_briefing_aggregates_7d_window(session):
    """최근 7일 창 집계 — 창 밖 실행 제외·FAILED 상세·overdue 목록·신호 포함."""
    now = datetime(2026, 7, 20, 9, 0)          # 월요일
    await _task(session, code="w1", cadence="DAILY")
    session.add_all([
        RecurringTaskRun(task_code="w1", period_key="2026-07-18", status="DONE",
                         scheduled_for=datetime(2026, 7, 18, 5, 0),
                         finished_at=datetime(2026, 7, 18, 5, 1)),
        RecurringTaskRun(task_code="w1", period_key="2026-07-19", status="FAILED",
                         scheduled_for=datetime(2026, 7, 19, 5, 0),
                         finished_at=datetime(2026, 7, 19, 5, 1), error="boom"),
        RecurringTaskRun(task_code="w1", period_key="2026-07-01", status="DONE",   # 창 밖
                         scheduled_for=datetime(2026, 7, 1, 5, 0),
                         finished_at=datetime(2026, 7, 1, 5, 1)),
    ])
    await session.flush()
    b = await runner.weekly_briefing(session, now=now)
    assert b["runs_7d"] == {"done": 1, "failed": 1}
    assert b["failed_7d"] == [{"code": "w1", "period_key": "2026-07-19", "error": "boom"}]
    assert "signal" in b and b["window"]["to"] == "2026-07-20"


async def test_weekly_briefing_handler_notifies_admins(session):
    """common.weekly_briefing — ADMIN·DIRECTOR 알림(body 디둡)·run.result=전체 브리핑."""
    from tests._http import make_user

    import erp.app.recurring_handlers  # noqa: F401
    from erp.common.models import Notification
    await make_user(session, user_id=61, role="ADMIN")
    await make_user(session, user_id=62, role="DIRECTOR")
    await make_user(session, user_id=63, role="STAFF")
    await _task(session, code="common.weekly_briefing", domain="common", cadence="WEEKLY",
                handler_ref="common.weekly_briefing",
                created_at=rec.now_naive())   # run_one은 실제 시계 — 유예로 signal 안정(디둡 전제)
    run = await runner.run_one(session, code="common.weekly_briefing", actor=1, force=True)
    assert run.status == "DONE", run.error
    assert run.result["notified"] == 2 and "runs_7d" in run.result
    rows = (await session.execute(select(Notification).where(
        Notification.type == "RECURRING_WEEKLY_BRIEF"))).scalars().all()
    assert {r.user_id for r in rows} == {61, 62}           # STAFF 제외
    # 재실행 — 동일 body 디둡(소음 없음)
    run2 = await runner.run_one(session, code="common.weekly_briefing", actor=1, force=True)
    assert run2.result["notified"] == 0


# ---- M3: AI_ASSISTED 스테이징(결정 대기함) + 컴플라이언스 링크아웃 ----

async def test_ai_assisted_prepare_then_approve(session):
    from erp.common.models import PendingDecision
    await _task(session, code="ai", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft")
    run = await runner.prepare_ai_assisted(session, code="ai", actor=3)
    assert run.status == "WAITING_APPROVAL"
    assert run.decision_key == f"rectask:ai:{run.period_key}"
    d = await session.scalar(select(PendingDecision).where(PendingDecision.key == run.decision_key))
    assert d is not None and d.status == "PENDING" and d.category == "OPS"
    # 재준비는 멱등(같은 run 반환·중복 결정항목 없음)
    assert (await runner.prepare_ai_assisted(session, code="ai", actor=3)).status == "WAITING_APPROVAL"
    # 승인 결정 → sync → DONE
    d.status, d.decided_option, d.decided_by = "DECIDED", "approve", 9
    await session.flush()
    out = await runner.sync_ai_decisions(session)
    assert out == {"done": 1, "skipped": 0}
    fin = await runner._get_run(session, "ai", run.period_key)
    assert fin.status == "DONE" and fin.actor == 9


async def test_ai_assisted_reject(session):
    from erp.common.models import PendingDecision
    await _task(session, code="ai", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft")
    run = await runner.prepare_ai_assisted(session, code="ai", actor=3)
    d = await session.scalar(select(PendingDecision).where(PendingDecision.key == run.decision_key))
    d.status, d.decided_option, d.decided_by = "DECIDED", "reject", 9
    await session.flush()
    await runner.sync_ai_decisions(session)
    assert (await runner._get_run(session, "ai", run.period_key)).status == "SKIPPED"


async def test_prepare_rejects_non_ai_mode(session):
    await _task(session, code="auto", execution_mode="AUTO")
    with pytest.raises(ValidationError):
        await runner.prepare_ai_assisted(session, code="auto", actor=1)


async def test_compliance_context_reuses_service(session):
    items = await runner.compliance_context(session)
    assert isinstance(items, list)      # 빈 DB에서도 예외 없이 동작(원천 서비스 재사용)


# ---- 기존 AUTO 잡 핸들러 이관(R9) ----

async def test_execute_duration_is_python_measured(session):
    """duration은 Python 캡처(실측) — PG timestamptz 라운드트립 offset(수시간) 왜곡 회귀 방지(prod F)."""
    from datetime import datetime, timezone
    await _task(session, code="t")
    run = await runner._ensure_run(session, code="t", pkey="2026-07", sched=datetime(2026, 7, 1))
    # started_at을 9시간 어긋난 aware 값으로 오염(과거 버그 상황 모사)
    run.status, run.started_at = "RUNNING", datetime.now(timezone.utc)
    await session.flush()
    task = await session.scalar(select(RecurringTask).where(RecurringTask.code == "t"))
    await runner._execute(session, task, run)
    assert run.status == "DONE" and run.duration_ms is not None
    assert 0 <= run.duration_ms < 300000    # 실측(~ms) — offset 수시간(3.24e7ms) 아님


_MIGRATED = ("scm.contract_lifecycle", "hr.annual_leave_grant", "patient.accrual",
             "claims.prereview_sync", "finance.balance_checkpoint", "finance.ledger_integrity",
             "finance.budget_alerts", "common.approval_ledger_verify", "hr.license_expiry",
             "analytics.bi_marts", "hr.resigned_access_revoke")
_MIGRATED_MONTHLY = ("itam.cost_review", "common.retention_sweep")
_NEW_AUTO = ("common.weekly_briefing",)          # 이관 아님·신규 AUTO(WEEKLY)


async def test_migrated_handlers_registered():
    import erp.app.recurring_handlers  # noqa: F401 — 등록 트리거
    for code in _MIGRATED + _MIGRATED_MONTHLY + _NEW_AUTO:
        assert rec.get_handler(code) is not None, f"핸들러 미등록: {code}"


async def test_seed_migrated_jobs(session):
    import erp.app.recurring_handlers  # noqa: F401
    n = await runner.seed_migrated_jobs(session)
    assert n == 15   # ops.consistency_audit 추가(v1.276)·hr.resigned_access_revoke(v1.288)
    tasks = {t["code"]: t for t in await runner.list_tasks(session)}
    for code in _MIGRATED + _MIGRATED_MONTHLY + _NEW_AUTO:
        t = tasks[code]
        assert t["execution_mode"] == "AUTO"
        assert t["cadence"] == ("MONTHLY" if code in _MIGRATED_MONTHLY
                                else "WEEKLY" if code in _NEW_AUTO else "DAILY")
        assert t["enabled"] is True and t["has_handler"] is True     # 컷오버 기본(모델 default)
    # cost_review 앵커 — 전월 기간의 실행시각=월말+3일 05:00(구 CronTrigger day=3, hour=5와 동일 날짜)
    cr = tasks["itam.cost_review"]
    assert rec.scheduled_for("MONTHLY", date(2026, 6, 15), cr["anchor"]) == datetime(2026, 7, 3, 5, 0)


async def test_seed_migrated_preserves_enabled(session):
    """재시드 footgun 없음 — enabled 미지정이라 기존 활성상태 보존."""
    import erp.app.recurring_handlers  # noqa: F401
    await runner.seed_migrated_jobs(session)
    await runner.upsert_task(session, code="patient.accrual", actor=1, enabled=False)  # 의도적 비활성
    await runner.seed_migrated_jobs(session)                                            # 재시드
    tasks = {t["code"]: t for t in await runner.list_tasks(session)}
    assert tasks["patient.accrual"]["enabled"] is False        # 보존


async def test_migrated_handler_executes_via_service(session):
    """실제 도메인 서비스 호출 이관 확인 — 빈 DB에서 멱등 no-op으로 DONE(스케줄러 잡과 동일 서비스)."""
    import erp.app.recurring_handlers  # noqa: F401
    for code in ("scm.contract_lifecycle", "finance.balance_checkpoint",
                 "finance.ledger_integrity", "finance.budget_alerts",
                 "common.approval_ledger_verify"):
        await _task(session, code=code, domain="finance", cadence="DAILY",
                    execution_mode="AUTO", handler_ref=code)
        run = await runner.run_one(session, code=code, actor=1, force=True)
        assert run.status == "DONE" and isinstance(run.result, dict), f"{code}: {run.error}"


@rec.register_handler("test.email_ok")
async def _h_email_ok(session, period_key, ctx):
    ctx["post_commit_emails"].append(("a@x.example", "제목", "본문"))
    return {"queued": 1}


@rec.register_handler("test.email_fail")
async def _h_email_fail(session, period_key, ctx):
    ctx["post_commit_emails"].append(("b@x.example", "제목", "본문"))
    raise RuntimeError("boom")


async def test_post_commit_email_sent_only_on_done(session, monkeypatch):
    """post-commit 이메일 채널 — DONE이면 커밋 후 발송·FAILED(롤백)면 미발송."""
    import erp.common.email as email_mod
    sent: list[tuple] = []
    monkeypatch.setattr(email_mod, "send_email", lambda to, s, b: sent.append((to, s, b)) or True)
    await _task(session, code="e1", handler_ref="test.email_ok", cadence="DAILY")
    await _task(session, code="e2", handler_ref="test.email_fail", cadence="DAILY")
    summary = await runner.run_due(session)
    assert summary["done"] == 1 and summary["failed"] == 1
    assert summary["emailed"] == 1 and [t for t, _, _ in sent] == ["a@x.example"]   # FAILED 미발송


async def test_license_expiry_handler_stages_email(session, monkeypatch):
    """hr.license_expiry — 인앱 경보 적재 + 이메일 스테이징(주소는 result 미저장·건수만)."""
    from datetime import timedelta

    import erp.app.recurring_handlers  # noqa: F401
    import erp.common.email as email_mod
    from erp.common.kst import kst_today
    from erp.common.models import Notification, User
    from erp.hr.models import Employee, License
    sent: list[tuple] = []
    monkeypatch.setattr(email_mod, "send_email", lambda to, s, b: sent.append((to, s, b)) or True)
    today = kst_today()
    e = Employee(id=1, employee_no="D001", name="김의사", hired_on=today - timedelta(days=1000))
    session.add(e)
    await session.flush()
    session.add(User(id=1, email="doc@x.example", password_hash="x", role="STAFF", employee_id=1))
    session.add(License(employee_id=1, license_type="의사", license_no="MD-1",
                        issued_on=today - timedelta(days=1000),
                        expires_on=today + timedelta(days=22)))    # 잔여 22일 → D-30 밴드
    await session.flush()
    await _task(session, code="hr.license_expiry", domain="hr", cadence="DAILY",
                handler_ref="hr.license_expiry")
    run = await runner.run_one(session, code="hr.license_expiry", actor=1, force=True)
    assert run.status == "DONE" and run.result["emails_queued"] == 1
    assert "emails" not in run.result                       # 주소 미저장
    assert sent == [("doc@x.example", "의사 면허 만료 임박(D-30)", f"MD-1 — 만료일 {today + timedelta(days=22)} (D-30)")]
    n = await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.type == "LICENSE_EXPIRY"))
    assert n == 1


async def test_bi_marts_handler_executes(session):
    """analytics.bi_marts — 빈 DB에서 전 마트 갱신 DONE·부분 실패는 result.failed로 가시화."""
    import erp.app.recurring_handlers  # noqa: F401
    await _task(session, code="analytics.bi_marts", domain="analytics", cadence="DAILY",
                handler_ref="analytics.bi_marts")
    run = await runner.run_one(session, code="analytics.bi_marts", actor=1, force=True)
    assert run.status == "DONE", run.error
    assert run.result["failed"] == [] and run.result["ok"] >= 21   # 12종×(당월·전월)+스냅샷·연간


async def test_migrated_monthly_handlers_execute(session):
    """월간 이관 2종(cost_review·retention_sweep) — 빈 DB 멱등 no-op으로 DONE(경보 0건)."""
    import erp.app.recurring_handlers  # noqa: F401
    from erp.common.models import Notification
    for code in _MIGRATED_MONTHLY:
        await _task(session, code=code, domain="common", cadence="MONTHLY",
                    execution_mode="AUTO", handler_ref=code)
        run = await runner.run_one(session, code=code, actor=1, force=True)
        assert run.status == "DONE" and isinstance(run.result, dict), f"{code}: {run.error}"
    n = await session.scalar(select(func.count()).select_from(Notification))
    assert n == 0                                          # 대상 0건 → 경보 없음


async def test_manual_overdue_reminder(session):
    """C — 지연 MANUAL 업무는 담당 역할에 리마인드(body 디둡·1회)."""
    from tests._http import make_user
    from erp.common.models import Notification
    await make_user(session, user_id=50, role="FINANCE_MANAGER")
    await _task(session, code="m", execution_mode="MANUAL", handler_ref=None, cadence="MONTHLY",
                owner_role="FINANCE_MANAGER", sla_days=1)
    now = datetime(2026, 7, 15, 12, 0)          # 7/1 + 1일 < 7/15 → overdue
    s1 = await runner.run_due(session, now=now)
    assert s1["reminded"] == 1
    n1 = await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.type == "RECURRING_TASK_DUE"))
    assert n1 == 1
    s2 = await runner.run_due(session, now=now)  # 재실행 → 디둡(추가 발송 없음)
    assert s2["reminded"] == 0
    n2 = await session.scalar(select(func.count()).select_from(Notification).where(
        Notification.type == "RECURRING_TASK_DUE"))
    assert n2 == 1


async def test_seed_starter_idempotent(session):
    n1 = await runner.seed_starter_tasks(session)
    n2 = await runner.seed_starter_tasks(session)      # 재실행 안전
    assert n1 == n2 and n1 >= 8
    codes = {t["code"] for t in await runner.list_tasks(session)}
    assert {"tax.vat_quarterly", "hr.year_end_settlement", "scm.stock_count_monthly"} <= codes
    # 부가세는 분기 종료 후 익월 25일 예정 — AI 보조 2호(초안까지만·신고는 사람)
    vat = next(t for t in await runner.list_tasks(session) if t["code"] == "tax.vat_quarterly")
    assert vat["cadence"] == "QUARTERLY" and vat["execution_mode"] == "AI_ASSISTED"


async def test_http_approval_required_rejected(session, client):
    from tests._http import auth_as
    await _task(session, code="appr", requires_approval=True)
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/admin/recurring-tasks/appr/run", headers=headers)
    assert r.status_code == 422       # ValidationError → 422


async def test_http_prepare_and_compliance_context(session, client):
    from tests._http import auth_as
    await _task(session, code="ai", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft")
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/admin/recurring-tasks/ai/prepare", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "WAITING_APPROVAL"
    assert r.json()["decision_key"].startswith("rectask:ai:")
    cc = await client.get("/api/v1/admin/recurring-tasks/compliance-context", headers=headers)
    assert cc.status_code == 200 and isinstance(cc.json()["deadlines"], list)


async def test_http_run_due_audit_label(session, client):
    """run-due EP(관리화면 버튼)는 triggered_by=ADMIN_UI로 감사(AI_CLI 아님 — F4)."""
    from tests._http import auth_as
    await _task(session, code="t", created_at=rec.now_naive())   # EP는 실제 시계 — 당월만 대상
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/admin/recurring-tasks/run-due", headers=headers)
    assert r.status_code == 200 and r.json()["done"] == 1
    runs = await client.get("/api/v1/admin/recurring-tasks/t/runs", headers=headers)
    assert runs.json()["runs"][0]["triggered_by"] == "ADMIN_UI"


# ---- PERIOD_END 앵커 기간 선정(적대검토 A-H1·A-H2 회귀) ----

_PERIOD_END_3D = {"basis": "PERIOD_END", "offset_days": 3, "hour": 5}


def test_due_period_picks_completed_period_for_period_end_anchor():
    """월말+3일 앵커: 7/28에 실행 대상은 '6월'(7/3 예정 도달)이지 7월이 아니다."""
    now = datetime(2026, 7, 28, 10, 0)
    pd = rec.due_period("MONTHLY", _PERIOD_END_3D, now)
    assert rec.period_key("MONTHLY", pd) == "2026-06"
    # 7월분은 8/3 05:00에야 대상이 된다
    assert rec.period_key("MONTHLY", rec.due_period(
        "MONTHLY", _PERIOD_END_3D, datetime(2026, 8, 3, 5, 0))) == "2026-07"


def test_due_period_none_before_first_schedule():
    """분기말+25일: 분기 첫날엔 소급 상한 안에 도달한 기간이 없으면 None."""
    assert rec.due_period("QUARTERLY", {"basis": "PERIOD_END", "offset_days": 25},
                          datetime(2026, 7, 1, 0, 0), lookback=0) is None


async def test_run_one_targets_due_period_not_today(session):
    """run_one이 '오늘이 속한 기간'을 잡아 정규 실행을 영구 차단하던 결함(A-H1)."""
    await _task(session, code="pe", cadence="MONTHLY", anchor=_PERIOD_END_3D)
    run = await runner.run_one(session, code="pe", actor=1)
    assert run.period_key == rec.period_key("MONTHLY", rec.due_period(
        "MONTHLY", _PERIOD_END_3D, rec.now_naive()))
    assert run.status == "DONE"
    # 아직 끝나지 않은 당월 키를 선점하지 않았어야 정규 실행이 살아 있다
    assert run.period_key != rec.period_key("MONTHLY", rec.now_naive().date())


async def test_run_due_default_catchup_materializes_period_end_anchor(session):
    """catchup 기본 0이라 관리화면/CLI 일괄 실행이 월말 앵커 업무를 건너뛰던 결함(A-H2)."""
    t = await _task(session, code="pe2", cadence="MONTHLY", anchor=_PERIOD_END_3D)
    t.created_at = datetime(2026, 1, 1)          # created_at 게이팅 통과
    await session.flush()
    summary = await runner.run_due(session, now=datetime(2026, 7, 28, 10, 0))
    assert summary["materialized"] == 1 and summary["done"] == 1
    run = (await session.scalars(select(RecurringTaskRun).where(
        RecurringTaskRun.task_code == "pe2"))).one()
    assert run.period_key == "2026-06"


# ---- 이상 신호 영구 발화 방지(적대검토 B-F1~F3 회귀) ----

async def _run_row(session, *, code, status, sched, started=None, finished=None, pkey="p"):
    r = RecurringTaskRun(task_code=code, period_key=pkey, status=status,
                         scheduled_for=sched, started_at=started, finished_at=finished)
    session.add(r)
    await session.flush()
    return r


async def test_failed_signal_clears_after_later_success(session):
    """실패 후 복구 성공했는데도 48h 내내 적색이던 결함(B-F3)."""
    now = datetime(2026, 7, 28, 12, 0)
    await _task(session, code="f1", cadence="DAILY")
    await _run_row(session, code="f1", status="FAILED", pkey="d1",
                   sched=now - timedelta(hours=10), finished=now - timedelta(hours=10))
    sig = await runner.health_signal(session, now=now)
    assert sig["failed_recent"] == ["f1"]
    await _run_row(session, code="f1", status="DONE", pkey="d2",
                   sched=now - timedelta(hours=1), finished=now - timedelta(hours=1))
    sig = await runner.health_signal(session, now=now)
    assert sig["failed_recent"] == [] and sig["ok"] is True


async def test_failed_signal_uses_finished_at_not_scheduled(session):
    """catchup 소급 실행의 실패는 scheduled_for가 오래됐다는 이유로 미탐이었다(B-F3)."""
    now = datetime(2026, 7, 28, 12, 0)
    await _task(session, code="f2", cadence="MONTHLY")
    await _run_row(session, code="f2", status="FAILED", pkey="2026-05",
                   sched=now - timedelta(days=60), finished=now - timedelta(minutes=5))
    assert (await runner.health_signal(session, now=now))["failed_recent"] == ["f2"]


async def test_stuck_due_ignores_stale_past_periods(session):
    """엔진이 방금 정상 물질화했는데도 과거 미완료 DUE 때문에 적색이던 결함(B-F2).

    MANUAL→AUTO 전환·재활성화처럼 과거 기간 DUE가 쌓인 정상 조작에서, 최신 기간이 제때
    물질화됐다면 엔진은 살아 있다 — 과거 행은 미실행 이력이지 '멈춤' 신호가 아니다.
    """
    now = datetime(2026, 7, 28, 12, 0)
    await _task(session, code="d1", cadence="DAILY", created_at=now)   # 유예 기준점 = 고정 now
    for i in range(4):                      # 과거 4일치 미완료 DUE
        await _run_row(session, code="d1", status="DUE", pkey=f"2026-07-2{i}",
                       sched=now - timedelta(days=5 - i))
    await _run_row(session, code="d1", status="DUE", pkey="2026-07-28",
                   sched=now - timedelta(minutes=30))          # 최신 기간은 정상
    sig = await runner.health_signal(session, now=now)
    assert sig["stuck_due"] == []
    assert sig["ok"] is True


async def test_reclaim_orphan_running_restores_due(session):
    """워커 크래시로 남은 RUNNING이 영구 잔류해 기간이 소실되던 결함(B-F1)."""
    now = datetime(2026, 7, 28, 12, 0)
    await _task(session, code="r1", cadence="MONTHLY")
    run = await _run_row(session, code="r1", status="RUNNING", pkey="2026-07",
                         sched=now - timedelta(hours=9), started=now - timedelta(hours=9))
    assert (await runner.health_signal(session, now=now))["stuck_running"] == ["r1"]
    assert (await runner.reclaim_orphan_runs(session))["reclaimed"] == 1
    await session.refresh(run)
    assert run.status == "DUE" and run.started_at is None and run.result["reclaimed"] is True
    assert (await runner.health_signal(session, now=now))["stuck_running"] == []


async def test_bi_marts_all_failed_raises_not_silent_done(session, monkeypatch):
    """전량 실패도 DONE이라 마트 stale이 신호에 안 잡히던 결함(A-M4)."""
    import erp.analytics.marts as marts

    async def _all_fail(_s, today=None):
        return {"ok": 0, "failed": ["a", "b", "c"]}

    monkeypatch.setattr(marts, "refresh_all_marts", _all_fail)
    await _task(session, code="bm", cadence="DAILY", handler_ref="analytics.bi_marts")
    run = await runner.run_one(session, code="bm", actor=1)
    assert run.status == "FAILED" and "전량" in (run.error or "")


async def test_bi_marts_partial_failure_stays_done(session, monkeypatch):
    """부분 실패는 원 잡의 '격리·계속'과 동치 — DONE + result.failed."""
    import erp.analytics.marts as marts

    async def _partial(_s, today=None):
        return {"ok": 5, "failed": ["a"]}

    monkeypatch.setattr(marts, "refresh_all_marts", _partial)
    await _task(session, code="bm2", cadence="DAILY", handler_ref="analytics.bi_marts")
    run = await runner.run_one(session, code="bm2", actor=1)
    assert run.status == "DONE" and run.result["failed"] == ["a"]


# ---- AI 보조 1호: 주간 미수금 점검(ops.ar_review_weekly) ----

async def test_ar_review_weekly_draft_has_no_side_effects(session):
    """초안 핸들러는 **읽기만** 한다 — 독촉·전표·채권 변경이 없어야 승인 전 상태가 안전하다."""
    from datetime import date

    from sqlalchemy import func, select as _sel

    from erp.app.recurring_handlers import ar_review_weekly
    from erp.finance.models import Receivable, Voucher

    session.add_all([
        Receivable(party="TEST-연체A", amount=1_000_000, collected_amount=0,
                   billed_on=date(2026, 1, 1), due_on=date(2026, 1, 31),
                   status="OUTSTANDING", source_module="manual"),
        Receivable(party="TEST-연체B", amount=300_000, collected_amount=100_000,
                   billed_on=date(2026, 6, 1), due_on=date(2026, 6, 30),
                   status="PARTIAL", source_module="manual"),
    ])
    await session.flush()
    before_v = await session.scalar(_sel(func.count()).select_from(Voucher))

    out = await ar_review_weekly(session, "2026-W31", {"mode": "prepare"})

    assert out["counts"]["candidates"] == 2
    assert out["counts"]["overdue_total"] == 1_200_000     # 1,000,000 + (300,000-100,000)
    assert "TEST-연체A" in out["question"]                  # 우선순위 상위가 초안에 실린다
    assert out["context_ref"] == "/patients/receivables"
    # 부작용 없음 — 전표 미생성·채권 상태 불변
    assert await session.scalar(_sel(func.count()).select_from(Voucher)) == before_v
    rows = (await session.scalars(_sel(Receivable))).all()
    assert {r.status for r in rows} == {"OUTSTANDING", "PARTIAL"}


async def test_ar_review_weekly_prepare_queues_decision(session):
    """prepare_ai_assisted가 결정 대기함에 큐잉하고 run을 WAITING_APPROVAL로 둔다(승인 전 미확정)."""
    from sqlalchemy import select as _sel

    from erp.common.models import PendingDecision

    await runner.upsert_task(session, code="ops.ar_review_weekly", actor=3,
                             name="주간 미수금 점검", domain="patient", cadence="WEEKLY",
                             execution_mode="AI_ASSISTED", handler_ref="ops.ar_review_weekly",
                             owner_role="FINANCE_MANAGER")
    run = await runner.prepare_ai_assisted(session, code="ops.ar_review_weekly", actor=3)
    assert run.status == "WAITING_APPROVAL" and run.decision_key
    d = await session.scalar(_sel(PendingDecision).where(PendingDecision.key == run.decision_key))
    assert d is not None and d.status == "PENDING" and "미수금" in d.title


async def test_seed_keeps_ar_review_ai_assisted(session):
    """시드 재실행이 AI_ASSISTED 전환을 되돌리지 않는다 — upsert가 기존 행을 덮어쓰기 때문."""
    from sqlalchemy import select as _sel

    from erp.common.models import RecurringTask

    await runner.seed_starter_tasks(session, actor=3)
    await runner.seed_starter_tasks(session, actor=3)      # 두 번 돌려도
    t = await session.scalar(_sel(RecurringTask).where(
        RecurringTask.code == "ops.ar_review_weekly"))
    assert t.execution_mode == "AI_ASSISTED" and t.handler_ref == "ops.ar_review_weekly"
    # HR 법정신고는 의도적으로 MANUAL 유지
    hr = await session.scalar(_sel(RecurringTask).where(
        RecurringTask.code == "hr.withholding_tax"))
    assert hr.execution_mode == "MANUAL"


# ---- AI 보조 2호: 부가세 신고 초안(tax.vat_quarterly) ----

async def test_vat_quarterly_draft_has_no_side_effects(session):
    """초안 핸들러는 vat_summary(안분 전 raw)를 읽기만 한다 — 전표·인보이스 상태 불변."""
    from datetime import date

    from sqlalchemy import func, select as _sel

    from erp.app.recurring_handlers import vat_quarterly_draft
    from erp.finance.models import TaxInvoice, Voucher

    session.add_all([
        TaxInvoice(approval_no="A1", invoice_type="TAX", direction="OUT",
                   issue_date=date(2026, 5, 10), supplier_bizno="1", supply_amount=1_000_000,
                   vat_amount=100_000, total_amount=1_100_000),
        TaxInvoice(approval_no="A2", invoice_type="EXEMPT", direction="OUT",
                   issue_date=date(2026, 6, 1), supplier_bizno="1", supply_amount=5_000_000,
                   vat_amount=0, total_amount=5_000_000),
        TaxInvoice(approval_no="A3", invoice_type="TAX", direction="IN",
                   issue_date=date(2026, 4, 2), supplier_bizno="2", supply_amount=300_000,
                   vat_amount=30_000, total_amount=330_000),
        TaxInvoice(approval_no="A4", invoice_type="TAX", direction="OUT",       # VOID 제외
                   issue_date=date(2026, 5, 20), supplier_bizno="1", supply_amount=9_999_999,
                   vat_amount=999_999, total_amount=10_999_998, status="VOID"),
        TaxInvoice(approval_no="A5", invoice_type="TAX", direction="OUT",       # 분기 밖 제외
                   issue_date=date(2026, 7, 1), supplier_bizno="1", supply_amount=7_777_777,
                   vat_amount=777_777, total_amount=8_555_554),
    ])
    await session.flush()
    before_v = await session.scalar(_sel(func.count()).select_from(Voucher))

    out = await vat_quarterly_draft(session, "2026-Q2", {"mode": "prepare"})

    assert out["counts"] == {"output_vat": 100_000, "input_vat": 30_000,
                             "payable_estimate": 70_000, "invoices": 3}
    assert "면세 매출" in out["question"] and "5,000,000" in out["question"]
    assert "신고·납부는 실행되지 않습니다" in out["question"]     # 승인 의미 오인 방지(책임 명시)
    assert out["context_ref"] == "/tax"
    # 부작용 없음 — 전표 미생성·인보이스 상태 불변
    assert await session.scalar(_sel(func.count()).select_from(Voucher)) == before_v
    assert await session.scalar(_sel(func.count()).select_from(TaxInvoice).where(
        TaxInvoice.status == "VOID")) == 1


async def test_prepare_targets_due_period_not_today(session):
    """prepare가 '오늘이 속한 기간'을 잡던 결함 — PERIOD_END 앵커에서 미완결 기간을 선점해
    정규 사이클을 영구 차단한다(run_one의 A-H1과 동일 패턴·due_period로 통일)."""
    await _task(session, code="ai-pe", cadence="MONTHLY", execution_mode="AI_ASSISTED",
                handler_ref="test.ai_draft", anchor=_PERIOD_END_3D)
    run = await runner.prepare_ai_assisted(session, code="ai-pe", actor=1)
    assert run.status == "WAITING_APPROVAL"
    assert run.period_key == rec.period_key("MONTHLY", rec.due_period(
        "MONTHLY", _PERIOD_END_3D, rec.now_naive()))
    # 아직 끝나지 않은 당월 키를 선점하지 않았어야 정규 실행이 살아 있다
    assert run.period_key != rec.period_key("MONTHLY", rec.now_naive().date())


async def test_run_due_auto_prepares_ai_draft_and_notifies(session):
    """AI 초안은 사람이 버튼을 누를 때까지 만들어지지 않았다 — 메타잡이 준비하고 담당에 알린다.

    prod 실측 근거: 1호 첫 주 run이 DUE로 방치돼 있었고(초안 없음), 담당자는 '지연' 알림만 받았다.
    """
    from tests._http import make_user
    from erp.common.models import Notification, PendingDecision

    await make_user(session, user_id=70, role="FINANCE_MANAGER")
    await _task(session, code="aiw", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft",
                cadence="MONTHLY", owner_role="FINANCE_MANAGER", sla_days=30)
    now = datetime(2026, 7, 15, 12, 0)
    s = await runner.run_due(session, now=now)

    assert s["prepared"] == 1 and s["notified"] == 1 and s["executed"] == 0
    run = await runner._get_run(session, "aiw", "2026-07")
    assert run.status == "WAITING_APPROVAL" and run.triggered_by == "SCHEDULER"
    assert await session.scalar(select(PendingDecision).where(
        PendingDecision.key == "rectask:aiw:2026-07")) is not None
    note = await session.scalar(select(Notification).where(
        Notification.type == "RECURRING_AI_DRAFT"))
    assert note is not None and note.href == "/admin/decisions" and note.user_id == 70
    # 재실행 멱등 — 이미 WAITING_APPROVAL이라 초안·알림이 늘지 않는다
    s2 = await runner.run_due(session, now=now)
    assert s2["prepared"] == 0 and s2["notified"] == 0


async def test_shadow_dry_run_does_not_prepare_draft(session):
    """섀도우(dry_run)는 물질화만 — 초안도 만들지 않는다(결정 대기함 오염 방지)."""
    from erp.common.models import PendingDecision

    await _task(session, code="aid", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft",
                cadence="MONTHLY")
    s = await runner.run_due(session, now=datetime(2026, 7, 15, 12, 0), dry_run=True)
    assert s["prepared"] == 0 and s["materialized"] == 1
    assert (await runner._get_run(session, "aid", "2026-07")).status == "DUE"
    assert await session.scalar(select(func.count()).select_from(PendingDecision)) == 0


async def test_failed_draft_is_retried_not_stuck(session):
    """실패한 초안이 재시도 불가로 굳던 결함 — `_claim`이 DUE에서만 전이하기 때문.

    자동 준비가 켜지면 이 실패는 아무도 안 보는 시각에 일어나고, FAILED는 DUE도
    WAITING_APPROVAL도 아니라 리마인드조차 닿지 않는 조용한 구멍이 됐다.
    """
    calls = {"n": 0}

    @rec.register_handler("test.draft_flaky")
    async def _flaky(session, period_key, ctx):        # noqa: ARG001
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("초안 원천 일시 오류")
        return {"summary": "ok", "question": "승인?"}

    await _task(session, code="aif", execution_mode="AI_ASSISTED",
                handler_ref="test.draft_flaky", cadence="MONTHLY")
    now = datetime(2026, 7, 15, 12, 0)
    s1 = await runner.run_due(session, now=now)
    assert s1["prepared"] == 0 and s1["failed"] == 1
    assert (await runner._get_run(session, "aif", "2026-07")).status == "FAILED"
    # 다음 틱에 재시도 → 성공(수동 "초안 준비"도 같은 경로를 쓴다)
    s2 = await runner.run_due(session, now=now)
    assert s2["prepared"] == 1
    run = await runner._get_run(session, "aif", "2026-07")
    assert run.status == "WAITING_APPROVAL" and run.error is None


async def test_manual_prepare_retries_failed_draft(session):
    """화면의 '초안 준비'가 FAILED run에 대해 조용한 no-op이던 것(성공 토스트만 떴다)."""
    @rec.register_handler("test.draft_ok2")
    async def _ok(session, period_key, ctx):          # noqa: ARG001
        return {"summary": "ok", "question": "승인?"}

    await _task(session, code="aim", execution_mode="AI_ASSISTED",
                handler_ref="test.draft_ok2", cadence="MONTHLY")
    run = await runner._ensure_run(session, code="aim", pkey=rec.period_key(
        "MONTHLY", rec.now_naive().date()), sched=rec.now_naive())
    run.status, run.error = "FAILED", "이전 실패"
    await session.flush()
    out = await runner.prepare_ai_assisted(session, code="aim", actor=5)
    assert out.status == "WAITING_APPROVAL" and out.error is None


async def test_ai_overdue_reminder_points_to_decisions_not_complete(session):
    """AI_ASSISTED 지연 알림이 '완료 처리 필요'라고 안내하던 것 — 그 모드는 완료처리가 거부된다."""
    from tests._http import make_user
    from erp.common.models import Notification

    await make_user(session, user_id=71, role="FINANCE_MANAGER")
    # 핸들러 없음 → 자동 준비 대상이 아니고, 기한 경과라 리마인드 경로로 간다
    await _task(session, code="ain", execution_mode="AI_ASSISTED", handler_ref=None,
                cadence="MONTHLY", owner_role="FINANCE_MANAGER", sla_days=1)
    s = await runner.run_due(session, now=datetime(2026, 7, 15, 12, 0))
    assert s["reminded"] == 1
    note = await session.scalar(select(Notification).where(
        Notification.type == "RECURRING_TASK_DUE"))
    assert note.href == "/admin/decisions" and "완료 처리" not in note.body
    # 완료처리는 실제로 거부되는 경로다 — 알림이 가리켜서는 안 된다
    with pytest.raises(ValidationError):
        await runner.complete_manual(session, code="ain", actor=1)


async def test_unstaffed_owner_role_falls_back_to_admin_with_reason(session):
    """담당 역할에 사람이 0명이면 알림이 **조용히 유실**됐다 — 발송 0건을 성공으로 돌려줬다.

    prod 실측(2026-07-30): 활성 업무의 FINANCE_MANAGER·LOGISTICS_MANAGER 보유자가 0명이라
    AI 초안 준비 알림·SLA 리마인드가 아무에게도 가지 않았다.
    """
    from tests._http import make_user
    from erp.common.models import Notification

    await make_user(session, user_id=74, role="ADMIN")       # FINANCE_MANAGER는 0명
    await _task(session, code="aiu", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft",
                cadence="MONTHLY", owner_role="FINANCE_MANAGER", sla_days=30)
    s = await runner.run_due(session, now=datetime(2026, 7, 15, 12, 0))

    assert s["prepared"] == 1 and s["notified"] == 1        # 유실되지 않는다
    note = await session.scalar(select(Notification).where(
        Notification.type == "RECURRING_AI_DRAFT"))
    assert note.user_id == 74                               # ADMIN 폴백
    assert "FINANCE_MANAGER 미배정" in note.body            # 원인이 받는 사람에게 드러난다


async def test_waiting_approval_reminded_even_when_period_gated(session):
    """승인 대기 리마인드가 **기간 루프에 갇혀** 있으면 닿지 않는 대기 건이 생긴다.

    prod 실측(2026-07-30): 수동 준비된 `tax.vat_quarterly` 2026-Q2 초안이 기한을 5일 넘겼는데도
    그 기간(종료 6/30 < 업무생성 7/27)을 루프가 created_at 게이팅으로 방문하지 않아 알림이 없었다.
    """
    from tests._http import make_user
    from erp.common.models import Notification

    await make_user(session, user_id=73, role="ADMIN")
    task = await _task(session, code="aig", execution_mode="AI_ASSISTED",
                       handler_ref="test.ai_draft", cadence="QUARTERLY",
                       owner_role="ADMIN", sla_days=5,
                       anchor={"basis": "PERIOD_END", "offset_days": 25})
    # 업무 생성 이전에 종료된 기간의 대기 건 — 루프는 이 기간을 백필하지 않는다(F3)
    run = await runner._ensure_run(session, code="aig", pkey="2026-Q2",
                                   sched=datetime(2026, 7, 25, 0, 0))
    run.status, run.decision_key = "WAITING_APPROVAL", "rectask:aig:2026-Q2"
    task.created_at = datetime(2026, 7, 27, 9, 0)
    await session.flush()

    s = await runner.run_due(session, now=datetime(2026, 7, 30, 18, 0))
    assert s["reminded"] == 1
    note = await session.scalar(select(Notification).where(
        Notification.type == "RECURRING_TASK_DUE"))
    assert note is not None and "2026-Q2" in note.body and note.href == "/admin/decisions"


async def test_waiting_approval_overdue_is_reminded(session):
    """승인 대기가 방치되면 알림이 하나도 없던 결함 — run_due가 continue로 빠졌다.

    승인되면 DONE이 되어 스스로 멈추는 조건부 알림이다(영구 발화 금지 규율).
    """
    from tests._http import make_user
    from erp.common.models import Notification, PendingDecision

    await make_user(session, user_id=72, role="FINANCE_MANAGER")
    await _task(session, code="aiq", execution_mode="AI_ASSISTED", handler_ref="test.ai_draft",
                cadence="MONTHLY", owner_role="FINANCE_MANAGER", sla_days=1)
    prep = datetime(2026, 7, 1, 9, 0)                 # 초안 준비(기한 07-02 이전)
    s0 = await runner.run_due(session, now=prep)
    assert (s0["prepared"], s0["reminded"]) == (1, 0)  # 기한 내에는 재촉하지 않는다
    late = datetime(2026, 7, 15, 12, 0)               # 07-01 + 1일 < 07-15 → 승인 지연
    s = await runner.run_due(session, now=late)
    assert s["reminded"] == 1
    note = await session.scalar(select(Notification).where(
        Notification.type == "RECURRING_TASK_DUE"))
    assert "승인" in note.body and note.href == "/admin/decisions"
    # 승인하면 DONE → 더 이상 리마인드되지 않는다(스스로 멈춘다)
    d = await session.scalar(select(PendingDecision).where(
        PendingDecision.key == "rectask:aiq:2026-07"))
    d.status, d.decided_option, d.decided_by = "DECIDED", "approve", 9
    await session.flush()
    s2 = await runner.run_due(session, now=late)
    assert s2["reminded"] == 0
    assert (await runner._get_run(session, "aiq", "2026-07")).status == "DONE"


async def test_seed_keeps_vat_quarterly_ai_assisted(session):
    """시드 재실행이 2호 전환을 되돌리지 않는다(_AI_ASSISTED 맵 미반영 시 조용히 MANUAL 원복)."""
    from sqlalchemy import select as _sel

    from erp.common.models import RecurringTask

    await runner.seed_starter_tasks(session, actor=3)
    await runner.seed_starter_tasks(session, actor=3)
    t = await session.scalar(_sel(RecurringTask).where(
        RecurringTask.code == "tax.vat_quarterly"))
    assert t.execution_mode == "AI_ASSISTED" and t.handler_ref == "tax.vat_quarterly"


async def test_list_tasks_exposes_owner_recipients(session):
    """담당 역할 보유자 수를 목록에 싣는다 — 0명이면 알림이 관리자 폴백으로만 간다.

    prod 실측(2026-07-30)에서 FINANCE_MANAGER·LOGISTICS_MANAGER가 0명이었는데 **그 사실이 화면
    어디에도 없어** 알림 유실을 몰랐다. 운영이 스스로 보고 담당을 바꿀 수 있어야 한다.
    """
    from tests._http import make_user

    await make_user(session, user_id=80, role="ADMIN")
    await make_user(session, user_id=81, role="ADMIN")
    await _task(session, code="own1", owner_role="ADMIN")
    await _task(session, code="own2", owner_role="FINANCE_MANAGER")     # 보유자 없음
    await _task(session, code="own3", owner_role=None)
    rows = {t["code"]: t for t in await runner.list_tasks(session)}
    assert rows["own1"]["owner_recipients"] == 2
    assert rows["own2"]["owner_recipients"] == 0
    assert rows["own3"]["owner_recipients"] == 0                        # 미지정도 0


async def test_owner_role_change_via_upsert(session):
    """담당 역할을 관리화면에서 바꿀 수 있다(PUT 경유 upsert_task) — 역할 배정은 /admin/users."""
    from tests._http import make_user

    await make_user(session, user_id=82, role="MANAGER")
    await _task(session, code="ownc", owner_role="FINANCE_MANAGER")
    await runner.upsert_task(session, code="ownc", actor=1, owner_role="MANAGER")
    rows = {t["code"]: t for t in await runner.list_tasks(session)}
    assert rows["ownc"]["owner_role"] == "MANAGER" and rows["ownc"]["owner_recipients"] == 1


async def test_monthly_auto_failure_retried_after_an_hour(session):
    """2026-10-09 검토 F — 월간 AUTO 실패는 1시간 뒤 재시도(종전 그 달 영구 미실행)."""
    from datetime import timedelta
    await _task(session, code="mret", handler_ref="test.auto_fail", cadence="MONTHLY")
    t0 = datetime(2026, 7, 2, 9, 0)
    await runner.run_due(session, now=t0)
    run = (await session.scalars(select(RecurringTaskRun).where(
        RecurringTaskRun.task_code == "mret"))).first()
    assert run.status == "FAILED"
    run.finished_at = t0
    await session.flush()
    rec._HANDLERS["test.auto_fail"] = _h_ok
    try:
        await runner.run_due(session, now=t0 + timedelta(minutes=30))   # 1시간 전 — 재시도 안 함
        await session.refresh(run)
        assert run.status == "FAILED"
        await runner.run_due(session, now=t0 + timedelta(hours=2))
        await session.refresh(run)
        assert run.status == "DONE" and run.result.get("_attempts") == 2
    finally:
        rec._HANDLERS["test.auto_fail"] = _h_fail
