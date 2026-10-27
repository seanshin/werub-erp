"""2026-10-10 연말·2027 전환 대비(v1.296.0) — 수가 버전·연말정산 API·FY 결산 가드·연간 정기업무."""
from datetime import UTC, date, datetime

from sqlalchemy import select

from erp.hr.models import CompensationProfile, Employee, PayrollLine, PayrollRecord


async def test_noncovered_sync_versions_by_year_and_keeps_history(session, monkeypatch):
    """2027 가격 변경이 2026 버전을 덮어쓰지 않는다(종전 valid_from=2026-01-01 고정)."""
    from erp.common import kst
    from erp.integrations import his_billing
    from erp.regulatory.models import FeeMaster
    price = {"v": 10_000}

    async def _pull(path):
        return [{"code": "CT-1", "name": "CT", "price": price["v"]}]
    monkeypatch.setattr(his_billing, "_pull", _pull)
    monkeypatch.setattr(kst, "kst_today", lambda: date(2026, 10, 10))
    assert (await his_billing.sync_noncovered(session))["created"] == 1
    monkeypatch.setattr(kst, "kst_today", lambda: date(2027, 1, 5))
    r = await his_billing.sync_noncovered(session)          # 가격 같음 — 새 버전 불필요
    assert r["created"] == 0 and r["updated"] == 0
    price["v"] = 12_000
    assert (await his_billing.sync_noncovered(session))["created"] == 1
    price["v"] = 12_500
    assert (await his_billing.sync_noncovered(session))["updated"] == 1   # 올해 버전만 갱신
    rows = {r.valid_from: (float(r.unit_price), r.valid_to) for r in (await session.scalars(
        select(FeeMaster).where(FeeMaster.code == "CT-1"))).all()}
    # 옛 버전은 새 버전 시작일에 닫힌다 — 열린 버전이 둘이면 as-of 조회가 2026 가격을 쓸 수 있다(검토 HIGH)
    assert rows == {date(2026, 1, 1): (10_000, date(2027, 1, 1)), date(2027, 1, 1): (12_500, None)}


async def test_year_end_uses_confirmed_payroll_and_profile_dependents(session, client):
    from tests._http import auth_as
    e = Employee(employee_no="YE-1", name="정산", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    session.add(CompensationProfile(employee_id=e.id, base_pay=3_000_000, dependents=3))
    for ym, conf in (("2025-11", True), ("2025-12", False), ("2025-10", False)):
        rec = PayrollRecord(employee_id=e.id, yyyymm=ym, confirmed_at=datetime.now(UTC) if conf else None)
        session.add(rec)
        await session.flush()
        if ym == "2025-10":   # 급여 없는 달의 성과급(미확정 레코드) — ESS처럼 합산
            session.add_all([PayrollLine(record_id=rec.id, item_code="INCENTIVE", item_kind="EARNING", amount=1_000_000),
                             PayrollLine(record_id=rec.id, item_code="INCTAX", item_kind="DEDUCTION", amount=30_000)])
            continue
        session.add_all([PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
                         PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=50_000)])
    await session.flush()
    h = await auth_as(session, user_id=971, role="ADMIN")
    r = await client.get(f"/api/v1/hr/employees/{e.id}/year-end?year=2025&children=1", headers=h)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["총급여"] == 4_000_000 and d["기납부세액"] == 80_000      # 미확정 12월 제외·10월 성과급 포함
    assert d["dependents"] == 3 and d["dependents_source"] == "보수 프로필"
    d2 = (await client.get(f"/api/v1/hr/employees/{e.id}/year-end?year=2025&dependents=1", headers=h)).json()
    assert d2["dependents"] == 1 and d2["dependents_source"] == "입력"


async def test_fiscal_year_close_rejected_before_year_end(session, client, monkeypatch):
    from erp.common import kst
    from tests._http import auth_as
    h = await auth_as(session, user_id=972, role="ADMIN")
    monkeypatch.setattr(kst, "kst_today", lambda: date(2026, 12, 31))
    r = await client.post("/api/v1/finance/closing/2026", headers=h)
    assert r.status_code == 422 and "2026-12-31" in r.text
    monkeypatch.setattr(kst, "kst_today", lambda: date(2027, 1, 2))
    r = await client.post("/api/v1/finance/closing/2026", headers=h)
    assert "회계연도는" not in r.text            # 가드 통과(손익 없음 422는 서비스 판단)


async def test_yearly_statutory_tasks_seeded():
    from erp.app.recurring_service import RecurringRunner
    codes = {c[0]: c for c in RecurringRunner._STARTER}
    for code in ("common.holidays_next_year", "hr.employer_insurance_rates", "tax.business_status_report",
                 "tax.payment_statements", "hr.insurance_total_wage_report", "finance.fiscal_year_close"):
        assert codes[code][3] == "YEARLY", code
    from erp.common import recurring as rec
    t = codes["tax.payment_statements"]
    assert rec.scheduled_for("YEARLY", date(2027, 6, 1), t[7]).date() == date(2027, 3, 10)   # 2026 귀속 지급명세서


async def test_task_created_after_due_does_not_materialize_past_period(session):
    """연간 업무를 10월에 신설해도 이미 지난 2026-02-10 기한분을 '지연'으로 만들지 않는다(검토 MED)."""
    from datetime import datetime as _dt

    from erp.app.recurring_service import RecurringRunner
    from erp.common.models import RecurringTask, RecurringTaskRun as RecurringRun
    t = RecurringTask(code="tax.bs_t", name="사업장현황신고", domain="tax", cadence="YEARLY",
                      category="FIXED", execution_mode="MANUAL", owner_role="ADMIN", sla_days=10,
                      anchor={"basis": "PERIOD_START", "offset_days": 40}, enabled=True,
                      created_at=_dt(2026, 10, 10, 9, 0, tzinfo=UTC))
    session.add(t)
    await session.flush()
    await RecurringRunner().run_due(session, now=_dt(2026, 10, 11, 9, 0))
    runs = (await session.scalars(select(RecurringRun).where(RecurringRun.task_code == "tax.bs_t"))).all()
    assert runs == []
    row = next(r for r in await RecurringRunner().due_list(session, now=_dt(2026, 10, 11, 9, 0))
               if r["code"] == "tax.bs_t")
    assert row["status"] == "NOT_APPLICABLE" and row["overdue"] is False   # 현황·주간 브리핑도 지연 아님(재검토 MED)
    await RecurringRunner().run_due(session, now=_dt(2027, 2, 11, 9, 0))
    runs = (await session.scalars(select(RecurringRun).where(RecurringRun.task_code == "tax.bs_t"))).all()
    assert [r.period_key for r in runs] == ["2027"]
