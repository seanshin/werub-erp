"""2026-10-10 — 결정 대기함의 정기업무 AI 초안 일괄 생략(prod 37건 중 12건이 쌓인 초안이라 진짜 결정을 가림).

전부 아니면 전무 · rectask:* PENDING만 · 일괄 승인은 없음(승인=검토 완료) · 생략된 run은 sync_ai_decisions가 SKIPPED.
또 다음 해 법정 요율 등록 정기업무(12월)가 시드된다.
"""
from sqlalchemy import select

from erp.audit.models import AuditLog
from erp.common.models import PendingDecision, RecurringTaskRun
from tests._http import auth_as

_OPTS = [{"key": "approve", "label": "승인", "recommended": True}, {"key": "reject", "label": "반려"}]


def _d(key, status="PENDING", opts=_OPTS):
    return PendingDecision(key=key, category="OPS", title=key, question="q", options=opts, status=status)


async def test_bulk_skip_all_or_nothing_and_sync(client, session):
    from datetime import datetime

    from erp.app.recurring_service import RecurringRunner
    admin = await auth_as(session, user_id=801, role="ADMIN")
    staff = await auth_as(session, user_id=802, role="STAFF")
    w31, w32, w41 = (_d(f"rectask:ops.ar_review_weekly:2026-W{w}") for w in (31, 32, 41))
    policy = _d("frontdesk-x", opts=[{"key": "a", "label": "a"}, {"key": "reject", "label": "r"}])
    done = _d("rectask:ops.ar_review_weekly:2026-W30", status="DECIDED")
    session.add_all([w31, w32, w41, policy, done])
    await session.flush()
    session.add(RecurringTaskRun(task_code="ops.ar_review_weekly", period_key="2026-W31",
                                 scheduled_for=datetime(2026, 7, 28), status="WAITING_APPROVAL",
                                 decision_key=w31.key))
    await session.flush()
    url = "/api/v1/admin/decisions/bulk-skip"
    assert (await client.post(url, headers=staff, json={"ids": [w31.id], "note": "x"})).status_code == 403
    for bad in ([w31.id, policy.id], [w31.id, done.id]):
        r = await client.post(url, headers=admin, json={"ids": bad, "note": "대체"})
        assert r.status_code == 422, r.text
    await session.refresh(w31)
    assert w31.status == "PENDING"                                    # 하나도 안 바뀜
    assert (await client.post(url, headers=admin, json={"ids": [w31.id], "note": " "})).status_code == 422
    assert (await client.post(url, headers=admin, json={"ids": [999999], "note": "x"})).status_code == 422
    r = await client.post(url, headers=admin, json={"ids": [w32.id, w31.id], "note": "W41 초안으로 대체"})
    assert r.status_code == 200 and set(r.json()["skipped"]) == {w31.key, w32.key}
    for d in (w31, w32):
        await session.refresh(d)
        assert d.status == "DECIDED" and d.decided_option == "reject" and d.decided_by == 801
        assert d.decided_note == "[일괄 생략] W41 초안으로 대체"
    await session.refresh(w41)
    assert w41.status == "PENDING"                                    # 최신은 그대로
    n = (await session.scalars(select(AuditLog).where(AuditLog.action == "DECIDE",
                                                      AuditLog.target_id.in_([w31.key, w32.key])))).all()
    assert len(n) == 2
    out = await RecurringRunner().sync_ai_decisions(session)
    run = await session.scalar(select(RecurringTaskRun).where(RecurringTaskRun.decision_key == w31.key))
    assert out["skipped"] == 1 and run.status == "SKIPPED"


async def test_next_year_statutory_task_seeded_for_december(session):
    from datetime import date

    from erp.app.recurring_service import RecurringRunner
    from erp.common import recurring as rec
    r = RecurringRunner()
    await r.seed_starter_tasks(session)
    t = next(x for x in await r.list_tasks(session) if x["code"] == "hr.statutory_params_next_year")
    assert t["cadence"] == "YEARLY" and t["execution_mode"] == "MANUAL" and t["owner_role"] == "ADMIN"
    assert rec.scheduled_for("YEARLY", date(2026, 10, 10), t["anchor"]).date() == date(2026, 12, 11)
