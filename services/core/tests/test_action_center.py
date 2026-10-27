"""결정·조치함(2026-10-11) — 화면 조치는 현재 데이터로 계산되고 처리하면 사라진다 · 분류 단일 소스."""
from datetime import date

from sqlalchemy import select

from erp.app import action_center as ac
from erp.common.models import Holiday, PendingDecision, RecurringTask, User
from erp.hr.models import WithholdingTable
from erp.integrations.models import HisInboundEvent

TODAY = date(2026, 10, 11)


def _keys(r):
    return {a["key"] for a in r["actions"]}


async def _table(session, status="CANDIDATE", eff=date(2026, 3, 1)):
    t = WithholdingTable(effective_from=eff, status=status, title="별표 2", rows=[[770, 775, 0]],
                         child_deduction={}, checksum="x")
    session.add(t)
    await session.flush()
    return t


async def test_withholding_candidate_until_activated(session):
    t = await _table(session)
    r = await ac.screen_actions(session, today=TODAY)
    item = next(a for a in r["actions"] if a["key"] == "withholding-table-2026-03-01")
    assert item["category"] == "HR" and item["screen"] == "/admin/payroll-settings"
    t.status = "ACTIVE"
    await session.flush()
    assert "withholding-table-2026-03-01" not in _keys(await ac.screen_actions(session, today=TODAY))


async def test_holiday_candidates_until_reviewed(session, client):
    """후보(2026 선거일·추석 연휴 토요일)가 미등록·미검토면 묻고, 일부만 등록해도 검토 기록이 남으면 닫힌다(이견 제외 허용)."""
    from tests._http import auth_as
    item = next(a for a in (await ac.screen_actions(session, today=TODAY))["actions"] if a["key"] == "holiday-candidates-2026")
    assert item["count"] == 2 and "선거" in item["detail"]
    h = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/holidays/candidates?year=2026", headers=h)
    assert r.status_code == 200 and [i["registered"] for i in r.json()["items"]] == [False, False]
    bad = await client.post("/api/v1/holidays/candidates/2026/review", headers=h,
                            json={"dates": ["2026-12-31"], "source_note": "관공서의 공휴일에 관한 규정 대조"})
    assert bad.status_code == 422
    session.add(Holiday(on_date=date(2026, 9, 26), name="수기 등록 추석", kind="LUNAR"))   # 이미 있는 날은 덮지 않음
    await session.flush()
    r = await client.post("/api/v1/holidays/candidates/2026/review", headers=h,
                          json={"dates": ["2026-06-03"], "source_note": "관공서의 공휴일에 관한 규정 대조"})
    assert r.status_code == 200 and r.json()["excluded"] == [] and r.json()["registered"] == ["2026-06-03", "2026-09-26"]
    names = {d: n for d, n in (await session.execute(select(Holiday.on_date, Holiday.name))).all()}
    assert names[date(2026, 9, 26)] == "수기 등록 추석" and date(2026, 6, 3) in names
    assert "holiday-candidates-2026" not in _keys(await ac.screen_actions(session, today=TODAY))
    # 11월부터 이듬해 후보(2027)를 묻는다
    assert "holiday-candidates-2027" in _keys(await ac.screen_actions(session, today=date(2026, 11, 2)))


async def test_year_without_lunar_holidays_is_missing_even_with_election(session):
    """2028: 후보는 선거일뿐 — 선거일만 등록·검토해도 음력 명절이 없으면 '미등록'은 계속(종전: 아무 날짜 1건이면 닫힘)."""
    from erp.common.holidays import review_candidates
    await review_candidates(session, year=2028, register=[date(2028, 4, 12)], source_note="공직선거법 §34 대조", actor=1)
    keys = _keys(await ac.screen_actions(session, today=date(2028, 1, 3)))
    assert "holiday-candidates-2028" not in keys and "holidays-2028" in keys
    session.add(Holiday(on_date=date(2028, 1, 26), name="설날", kind="PUBLIC"))   # 수기 기본 종류여도 명칭으로 인정
    await session.flush()
    assert "holidays-2028" not in _keys(await ac.screen_actions(session, today=date(2028, 1, 3)))


def test_labor_and_constitution_day_fixed_from_2026():
    """노동절·제헌절 — 2026부터 고정 공휴일(급여·청구 공휴가산·기한 단일 판정)."""
    from erp.claims import deadline
    from erp.claims.pre_review import _is_surcharge_holiday
    assert deadline.is_fixed_holiday(date(2026, 5, 1)) and deadline.is_fixed_holiday(date(2027, 7, 17))
    assert not deadline.is_fixed_holiday(date(2025, 5, 1)) and not deadline.is_fixed_holiday(date(2025, 7, 17))
    assert _is_surcharge_holiday(date(2026, 7, 17)) and deadline.is_holiday(date(2026, 5, 1))


async def test_payroll_month_holidays_include_labor_day(session):
    from erp.hr.pay_inputs import _month_holidays
    assert date(2026, 5, 1) in await _month_holidays(session, 2026, 5)
    assert date(2025, 5, 1) not in await _month_holidays(session, 2025, 5)


async def test_statutory_current_year_does_not_go_quiet_on_jan_1(session, monkeypatch):
    """이듬해분만 보면 1/1에 항목이 사라진다 — 올해분 미등록은 언제나 뜬다."""
    import erp.hr.statutory as st

    async def _noop(session):
        return None
    monkeypatch.setattr(st, "refresh_db_params", _noop)
    monkeypatch.setattr(st, "registered_years", lambda: {"insurance": {2026}, "tax": {2026}})
    assert "statutory-2027" not in _keys(await ac.screen_actions(session, today=date(2026, 11, 30)))
    assert "statutory-2027" in _keys(await ac.screen_actions(session, today=date(2026, 12, 1)))
    item = next(a for a in (await ac.screen_actions(session, today=date(2027, 1, 2)))["actions"]
                if a["key"] == "statutory-2027")
    assert "멈춰 있습니다" in item["detail"]


async def test_withholding_candidate_discard_clears_item(session, client):
    from erp.audit.models import AuditLog
    from tests._http import auth_as
    t = await _table(session)
    h = await auth_as(session, user_id=1, role="ADMIN")
    url = f"/api/v1/hr/withholding-tables/{t.id}/discard"
    assert (await client.post(url, headers=h, json={"reason": "짧음"})).status_code == 422
    r = await client.post(url, headers=h, json={"reason": "원표 300만 구간 가족 2명 값 불일치"})
    assert r.status_code == 200 and r.json()["status"] == "RETIRED"
    assert (await client.post(url, headers=h, json={"reason": "원표 300만 구간 가족 2명 값 불일치"})).status_code == 422
    assert "withholding-table-2026-03-01" not in _keys(await ac.screen_actions(session, today=TODAY))
    assert await session.scalar(select(AuditLog).where(AuditLog.entity == "hr.withholding_tables",
                                                       AuditLog.action == "RETIRE"))


def test_appeal_deadline_keeps_code_holidays_when_db_has_some():
    """DB 캘린더에 선거일 하나만 있어도 코드 표의 설·추석은 기한 계산에 남는다(종전: DB가 코드를 대체)."""
    from erp.claims.deadline import is_holiday
    assert is_holiday(date(2026, 2, 17), {date(2026, 6, 3)})
    assert is_holiday(date(2026, 6, 3), {date(2026, 6, 3)})


async def test_inbound_dead_and_unstaffed_owner(session):
    session.add(HisInboundEvent(domain="finance", event_type="billing.changed", source_ref="E-1",
                                payload={}, status="DEAD"))
    session.add(RecurringTask(code="t.x", name="x", domain="finance", cadence="MONTHLY",
                              owner_role="FINANCE_MANAGER"))
    await session.flush()
    r = await ac.screen_actions(session, today=TODAY)
    dead = next(a for a in r["actions"] if a["key"] == "inbound-dead")
    assert dead["count"] == 1 and dead["category"] == "INTEGRATION"
    assert "recurring-owner-unstaffed" in _keys(r)
    # 비활성 계정만 있으면 여전히 미배정(알림 수신자 수·관리자 폴백과 같은 기준)
    u = User(email="fm@x.example", role="FINANCE_MANAGER", password_hash="x", is_active=False)
    session.add(u)
    await session.flush()
    assert "recurring-owner-unstaffed" in _keys(await ac.screen_actions(session, today=TODAY))
    from erp.app.recurring_service import RecurringRunner
    row = next(x for x in await RecurringRunner().list_tasks(session) if x["code"] == "t.x")
    assert row["owner_recipients"] == 0
    u.is_active = True
    await session.flush()
    assert "recurring-owner-unstaffed" not in _keys(await ac.screen_actions(session, today=TODAY))


async def test_probe_failure_is_reported_not_swallowed(session, monkeypatch):
    async def boom(session, today):
        raise RuntimeError("점검 실패 재현")
    monkeypatch.setattr(ac, "_PROBES", [("터짐", boom), ("공휴일", ac._holidays)])
    r = await ac.screen_actions(session, today=TODAY)
    assert r["errors"] and r["errors"][0]["probe"] == "터짐"
    assert "holiday-candidates-2026" in _keys(r)       # 나머지 점검은 그대로


async def test_actions_endpoint_rbac_and_categories(session, client):
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/decisions/actions", headers=admin)
    assert r.status_code == 200
    assert [c["key"] for c in r.json()["categories"]][:2] == ["HR", "FINANCE"]
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.get("/api/v1/admin/decisions/actions", headers=staff)).status_code == 403


async def test_category_validation_and_recategorize_decided(session, client):
    """분류는 단일 목록만 · 결정된 항목도 분류만은 옮길 수 있다(결정 근거 불변) · 감사로그."""
    from erp.audit.models import AuditLog
    from tests._http import auth_as
    h = await auth_as(session, user_id=1, role="ADMIN")
    body = {"key": "c-1", "title": "t", "question": "?", "options": [{"key": "A", "label": "A"}],
            "category": "misc"}
    assert (await client.post("/api/v1/admin/decisions", headers=h, json=body)).status_code == 422
    body["category"] = "hr"
    assert (await client.post("/api/v1/admin/decisions", headers=h, json=body)).status_code == 201
    d = await session.scalar(select(PendingDecision).where(PendingDecision.key == "c-1"))
    assert d.category == "HR"
    d.status, d.decided_option = "DECIDED", "A"
    await session.flush()
    r = await client.post(f"/api/v1/admin/decisions/{d.id}/category", headers=h, json={"category": "FINANCE"})
    assert r.status_code == 200 and r.json()["category"] == "FINANCE" and r.json()["decided_option"] == "A"
    assert (await client.post(f"/api/v1/admin/decisions/{d.id}/category", headers=h,
                              json={"category": "X"})).status_code == 422
    log = await session.scalar(select(AuditLog).where(AuditLog.entity == "common.pending_decisions",
                                                      AuditLog.target_id == "c-1", AuditLog.action == "UPDATE"))
    assert log is not None
