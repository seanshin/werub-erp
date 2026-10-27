"""v1.297.0 — 근로소득 간이세액표(별표 2·2026.3.1 시행) 후보→활성·급여 계산·영향 미리보기."""
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.hr import payroll_rules
from erp.hr import withholding_table as wht
from erp.hr.models import Employee, PayrollLine, PayrollRecord, WithholdingTable

ROWS = wht.read_csv(wht.DATA_DIR / "withholding_table_2026-03-01.csv")
T = {"id": 1, "effective_from": date(2026, 3, 1), "rows": ROWS, "child_deduction": wht.CHILD_2026}


def test_shipped_table_structure_and_known_cells():
    wht.validate(ROWS)
    assert len(ROWS) == 647 and ROWS[0][:2] == [770, 775] and ROWS[-1][:2] == [10_000, 10_000]
    row = next(r for r in ROWS if r[0] == 3_000)
    assert row[2:5] == [74_350, 56_850, 31_940]          # 월 300만~302만·가족 1/2/3명(별표 2 원문)
    assert ROWS[-1][2] == 1_507_400


@pytest.mark.parametrize("gross,fam,kids,tax", [
    (3_005_000, 1, 0, 74_350), (3_005_000, 2, 1, 56_850 - 20_830), (3_005_000, 2, 2, 56_850 - 45_830),
    (3_005_000, 1, 3, 0),                                  # 차감 후 음수 → 0
    (12_000_000, 3, 0, int(1_200_840 + 2_000_000 * 0.98 * 0.35 + 25_000)),
    (500_000, 1, 0, 0), (10_000_000, 1, 0, 1_507_400)])
def test_compute_rules(gross, fam, kids, tax):
    out = wht.compute(T, gross, family=fam, children=kids)
    assert out["소득세"] == tax // 10 * 10 and out["지방소득세"] == (out["소득세"] // 10) // 10 * 10


def test_over_11_family_rule():
    a = wht.compute(T, 8_050_000, family=11)["소득세"]
    b = wht.compute(T, 8_050_000, family=10)["소득세"]
    assert wht.compute(T, 8_050_000, family=13)["소득세"] == max(a - (b - a) * 2, 0)


def test_validate_rejects_broken_tables():
    broken = [r[:] for r in ROWS]
    broken[-1][3] = broken[-1][2] + 1                     # 1천만원 행: 가족 2명이 1명보다 큼(급여 단조 검사 대상 밖)
    with pytest.raises(ValidationError, match="가족 단조성"):
        wht.validate(broken)
    gap = ROWS[:50] + ROWS[51:]
    with pytest.raises(ValidationError, match="끊겼"):
        wht.validate(gap)


def test_build_payroll_uses_table_and_excludes_nontax():
    r = payroll_rules.build_payroll(base_pay=3_005_000, dependents=2, year=2026, month=10,
                                    withholding_table=T, children=1)
    tax = next(ln for ln in r["lines"] if ln["item_code"] == "TAX")
    assert tax["amount"] == 56_850 - 20_830
    assert tax["calc_basis"]["method"] == "간이세액표" and tax["calc_basis"]["children"] == 1
    r2 = payroll_rules.build_payroll(base_pay=3_005_000, dependents=2, year=2026, month=10)
    t2 = next(ln for ln in r2["lines"] if ln["item_code"] == "TAX")
    assert t2["calc_basis"]["method"] == "근사산식" and t2["amount"] > 56_850   # 표 없으면 종전 산식(더 큼)


async def _candidate(session, **kw) -> WithholdingTable:
    t = WithholdingTable(effective_from=kw.get("eff", date(2026, 3, 1)), status="CANDIDATE",
                         title="후보", rows=ROWS, child_deduction=wht.CHILD_2026, checksum=wht.checksum(ROWS))
    session.add(t)
    await session.flush()
    return t


async def test_activate_rules(session):
    t = await _candidate(session)
    with pytest.raises(ValidationError, match="8자 이상"):
        await wht.activate(session, actor=1, table_id=t.id, source_note="대조함")
    assert await wht.load_active(session, on=date(2026, 10, 1)) is None   # 후보는 계산에 안 쓴다
    await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 10칸 대조 일치")
    act = await wht.load_active(session, on=date(2026, 10, 1))
    assert act and act["id"] == t.id
    assert await wht.load_active(session, on=date(2026, 2, 1)) is None    # 시행일 전 급여월은 표 없음
    t2 = await _candidate(session)
    t2.rows = [r[:] for r in ROWS]
    t2.rows[0][2] = 1                                     # 등록 후 변조 → 체크섬 불일치
    await session.flush()
    with pytest.raises(ValidationError):
        await wht.activate(session, actor=1, table_id=t2.id, source_note="홈택스 원표 대조 완료")
    t3 = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t3.id, source_note="재대조 — 정정본 활성화")
    await session.refresh(t)
    assert t.status == "RETIRED"                          # 같은 시행일 기존 활성은 폐기


async def test_generate_after_activation_uses_table_with_children(session, client):
    from erp.hr.models import CompensationProfile, Dependent
    from tests._http import auth_as
    e = Employee(employee_no="WT-1", name="원천", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    session.add_all([CompensationProfile(employee_id=e.id, base_pay=3_005_000, dependents=2),
                     Dependent(employee_id=e.id, name="자녀", relation="CHILD", birth_date=date(2015, 5, 1),
                               counted=True),
                     Dependent(employee_id=e.id, name="영아", relation="CHILD", birth_date=date(2022, 5, 1),
                               counted=True)])                        # 2026년 4세 — 자녀 차감 제외
    t = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 대조 일치")
    h = await auth_as(session, user_id=981, role="ADMIN")
    r = await client.post("/api/v1/hr/payrolls/2026-10/generate", headers=h, json={"employee_id": e.id})
    assert r.status_code == 200, r.text
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == e.id))
    tax = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rec.id,
                                                         PayrollLine.item_code == "TAX"))
    # 공제대상가족 = 본인 + 등록 자녀 2명(4세도 기본공제 대상) = 3명 · 8~20세 자녀 차감은 1명
    base = next(x for x in ROWS if x[0] <= 3_005 < x[1])[4]
    assert tax.calc_basis["dependents"] == 3 and tax.calc_basis["children"] == 1
    assert int(tax.amount) == base - 20_830


async def test_impact_preview_measures_overwithholding(session, client):
    from erp.audit.models import AccessLog
    from tests._http import auth_as
    e = Employee(employee_no="WT-2", name="측정", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-07", confirmed_at=datetime.now(UTC))
    session.add(rec)
    await session.flush()
    session.add_all([PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_005_000),
                     PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=134_130,
                                 calc_basis={"method": "간이세액표", "dependents": 1})])
    t = await _candidate(session)
    h = await auth_as(session, user_id=982, role="ADMIN")
    d = (await client.get(f"/api/v1/hr/withholding-tables/{t.id}/impact?yyyymm=2026-07", headers=h)).json()
    assert d["records"] == 1 and d["diff_total"] == 74_350 - 134_130 and d["rows"][0]["table"] == 74_350
    assert (await session.scalars(select(AccessLog).where(
        AccessLog.target_id == "withholding-impact:2026-07"))).all()
    lst = (await client.get("/api/v1/hr/withholding-tables", headers=h)).json()["items"]
    assert lst[0]["status"] == "CANDIDATE" and len(lst[0]["samples"]) == 10


def test_bonus_tax_with_table_has_no_formula_gap():
    """A(월급+상여)와 C(월급)를 같은 표로 — A만 산식이면 표 기준 C와 섞여 상여가 0이어도 과다 징수(검토 MED)."""
    from erp.hr import statutory
    c = wht.compute(T, 3_005_000, family=1)["소득세"]
    zero = statutory.bonus_income_tax(3_005_000, 0, months=1, dependents=1, prepaid_monthly_tax=c, table=T)
    assert zero["소득세"] == 0
    t = statutory.bonus_income_tax(3_005_000, 1_000_000, months=1, dependents=1, prepaid_monthly_tax=c, table=T)
    assert t["소득세"] == wht.compute(T, 4_005_000, family=1)["소득세"] - c


async def test_bulk_and_single_generate_agree_on_family_and_children(session, client):
    """일괄 생성도 부양가족 등록으로 가족 수 — 종전 '프로필 1명 + 등록 자녀 2명 차감' 불가능 조합(검토 MED)."""
    from erp.hr.models import CompensationProfile, Dependent
    from tests._http import auth_as
    e = Employee(employee_no="WT-3", name="일괄", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    session.add_all([CompensationProfile(employee_id=e.id, base_pay=3_005_000, dependents=1),
                     Dependent(employee_id=e.id, name="배우자", relation="SPOUSE", counted=True),
                     Dependent(employee_id=e.id, name="자1", relation="CHILD", birth_date=date(2014, 1, 1), counted=True),
                     Dependent(employee_id=e.id, name="자2", relation="CHILD", birth_date=date(2016, 1, 1), counted=True)])
    t = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 대조 일치")
    h = await auth_as(session, user_id=983, role="ADMIN")
    r = await client.post("/api/v1/hr/payrolls/2026-11/generate-bulk", headers=h)
    assert r.status_code == 200, r.text
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == e.id,
                                                           PayrollRecord.yyyymm == "2026-11"))
    tax = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rec.id,
                                                         PayrollLine.item_code == "TAX"))
    assert tax is None or (tax.calc_basis["dependents"] == 4 and tax.calc_basis["children"] == 2)
    want = wht.compute(T, 3_005_000, family=4, children=2)["소득세"]       # 26,690 − 45,830 → 0
    assert want == 0 and (tax is None or int(tax.amount) == want)
    r = await client.post("/api/v1/hr/payrolls/2026-12/generate", headers=h, json={"employee_id": e.id})
    rec2 = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == e.id,
                                                            PayrollRecord.yyyymm == "2026-12"))
    tax2 = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rec2.id,
                                                          PayrollLine.item_code == "TAX"))
    assert (tax2 is None) == (tax is None)                 # 개별·일괄 같은 결과(세액 0이면 줄 없음)


async def test_impact_skips_daily_workers(session):
    e = Employee(employee_no="WT-4", name="일용", hired_on=date(2020, 1, 1), employment_type="DAILY",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-07", confirmed_at=datetime.now(UTC))
    session.add(rec)
    await session.flush()
    session.add_all([PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=2_000_000),
                     PayrollLine(record_id=rec.id, item_code="TAX", item_kind="DEDUCTION", amount=5_400,
                                 calc_basis={"method": "일용분리과세"})])
    t = await _candidate(session)
    d = await wht.impact(session, table_id=t.id, yyyymm="2026-07")
    assert d["records"] == 0


@pytest.mark.parametrize("tax_on_table", [False, True])
async def test_combined_bonus_tax_uses_same_basis_as_recorded_tax(session, tax_on_table):
    """성과급 합산 원천징수 A·C는 그 달 기록된 TAX 근거와 같게 — 활성화 전 산식 급여에 표 A를 쓰면 과소(재검토 MED)."""
    from erp.hr import statutory
    from erp.hr.payroll_settings import PayrollSettingsService
    from tests.test_incentive_workflow import _doctor_with_revenue, svc
    await PayrollSettingsService().upsert(session, actor=1, incentive_withholding_method="COMBINED", combined_months=1)
    d = await _doctor_with_revenue(session, f"DR-WT{int(tax_on_table)}", 5_000_000)   # 성과급 1,000,000
    t = await _candidate(session)
    if tax_on_table:
        await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 대조 일치")
    from erp.hr.employee_service import PayrollService
    tbl = {"id": t.id, "effective_from": t.effective_from, "rows": ROWS, "child_deduction": wht.CHILD_2026}
    lines = payroll_rules.build_payroll(base_pay=4_000_000, dependents=1, year=2026, month=6,
                                        withholding_table=tbl if tax_on_table else None)["lines"]
    await PayrollService().upsert_lines(session, actor=1, employee_id=d.id, yyyymm="2026-06", lines=lines)
    if not tax_on_table:     # 산식으로 생성된 뒤 표가 활성화된 상황
        await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 대조 일치")
    ic = await svc.register(session, actor=1, employee_id=d.id, period="2026-06")
    await svc.review(session, actor=2, calc_id=ic.id)
    await svc.approve(session, actor=3, calc_id=ic.id)
    rec = await session.scalar(select(PayrollRecord).where(PayrollRecord.employee_id == d.id,
                                                           PayrollRecord.yyyymm == "2026-06"))
    inctax = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rec.id,
                                                            PayrollLine.item_code == "INCTAX"))
    if tax_on_table:
        want = wht.compute(tbl, 5_000_000, family=1)["소득세"] - wht.compute(tbl, 4_000_000, family=1)["소득세"]
    else:
        f = lambda x: statutory.estimate_monthly_income_tax(x, dependents=1, year=2026)["소득세"]  # noqa: E731
        want = f(5_000_000) - f(4_000_000)
    assert inctax is not None and int(inctax.amount) == want // 10 * 10


# ── v1.299 — 활성화 전에 만든 미확정 급여의 소득세 줄 재계산(결정·조치함 항목 → 급여 설정 카드) ──────────
async def _rec(session, no, *, ym="2026-10", tax_basis=None, confirmed=False, extra=()):
    e = Employee(employee_no=no, name=no, hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    r = PayrollRecord(employee_id=e.id, yyyymm=ym, confirmed_at=datetime.now(UTC) if confirmed else None)
    session.add(r)
    await session.flush()
    session.add(PayrollLine(record_id=r.id, item_code="BASE", item_kind="EARNING", amount=3_005_000))
    if tax_basis is not None:
        session.add(PayrollLine(record_id=r.id, item_code="TAX", item_kind="DEDUCTION", amount=134_130,
                                calc_basis=tax_basis))
        session.add(PayrollLine(record_id=r.id, item_code="LOCALTAX", item_kind="DEDUCTION", amount=13_410))
    for code in extra:
        session.add(PayrollLine(record_id=r.id, item_code=code, item_kind="DEDUCTION", amount=1_000))
    await session.flush()
    return r


async def test_retax_unconfirmed_formula_records_only(session, client):
    from erp.app import action_center as ac
    from erp.audit.models import AuditLog
    from tests._http import auth_as
    t = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 10칸 대조 일치")
    formula = {"method": "근사산식", "dependents": 1, "taxable": 3_005_000}
    target = await _rec(session, "RT-1", tax_basis=formula)
    done = await _rec(session, "RT-2", tax_basis=formula, confirmed=True)          # 확정분 — 손대지 않음
    inc = await _rec(session, "RT-3", tax_basis=formula, extra=("INCTAX",))       # 성과급 합산 — 개별 재생성
    await _rec(session, "RT-4", tax_basis={"method": "일용분리과세"})               # 일용 — 대상 아님
    await _rec(session, "RT-5", ym="2026-02", tax_basis=formula)                  # 시행일 전 달 — 대상 아님
    # v1.297 이전 기록(과세 월급여액 없음·'간이세액표' 오표기 산식) — 지급 줄에서 복원해 대상
    old = await _rec(session, "RT-6", tax_basis={"formula": "간이세액표(부양 1인)", "method": "간이세액표", "dependents": 1})
    manual = await _rec(session, "RT-7", tax_basis=None)                          # 수기 PUT(근거 없음) — 개별 확인
    session.add(PayrollLine(record_id=manual.id, item_code="TAX", item_kind="DEDUCTION", amount=50_000))
    await session.flush()
    keys = {a["key"]: a for a in (await ac.screen_actions(session, today=date(2026, 10, 11)))["actions"]}
    assert keys[f"withholding-retax-{t.id}"]["count"] == 3     # 대상 2 + 성과급 합산 1(수기 RT-7은 세지 않음·영구 발화 방지)
    h = await auth_as(session, user_id=1, role="ADMIN")
    p = (await client.get(f"/api/v1/hr/withholding-tables/{t.id}/retax-preview", headers=h)).json()
    assert p["targets"] == 2 and {x["new"] for x in p["rows"]} == {74_350 + 7_430}
    assert p["diff_total"] == 2 * (74_350 + 7_430 - (134_130 + 13_410))
    assert {s["employee_no"]: s["reason"][:6] for s in p["skipped"]} == {"RT-3": "성과급 합산", "RT-7": "수기 입력 "}
    assert (await client.post(f"/api/v1/hr/withholding-tables/{t.id}/retax", headers=h,
                              json={"note": "x"})).status_code == 422
    r = await client.post(f"/api/v1/hr/withholding-tables/{t.id}/retax", headers=h, json={"note": "표 활성화 후 재계산"})
    assert r.status_code == 200 and r.json()["targets"] == 2
    old_tax = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == old.id, PayrollLine.item_code == "TAX"))
    assert old_tax.amount == 74_350 and old_tax.calc_basis["taxable"] == 3_005_000
    lines = {ln.item_code: ln for ln in (await session.scalars(
        select(PayrollLine).where(PayrollLine.record_id == target.id))).all()}
    assert lines["TAX"].amount == 74_350 and lines["TAX"].calc_basis["table_id"] == t.id
    assert lines["LOCALTAX"].amount == 7_430 and lines["BASE"].amount == 3_005_000
    for rid in (done.id, inc.id):
        tax = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rid, PayrollLine.item_code == "TAX"))
        assert tax.amount == 134_130
    left = {a["key"]: a for a in (await ac.screen_actions(session, today=date(2026, 10, 11)))["actions"]}
    assert left[f"withholding-retax-{t.id}"]["count"] == 1 and "개별 확인" in left[f"withholding-retax-{t.id}"]["title"]
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "RETAX"))


async def test_no_active_table_is_asked_even_after_last_candidate_discarded(session):
    from erp.app import action_center as ac
    t = await _candidate(session)
    keys = {a["key"] for a in (await ac.screen_actions(session, today=date(2026, 10, 11)))["actions"]}
    assert "withholding-table-2026-03-01" in keys and "withholding-table-none" not in keys
    await wht.discard(session, actor=1, table_id=t.id, reason="원표 300만 구간 값 불일치")
    keys = {a["key"] for a in (await ac.screen_actions(session, today=date(2026, 10, 11)))["actions"]}
    assert "withholding-table-none" in keys


async def test_retax_uses_generation_family_and_follows_new_table(session):
    """가족 수=생성과 같은 부양가족 등록 기준(기록의 옛 프로필 값 아님) · 다른 표(정정 전·옛 연도)로 계산된 줄도 대상."""
    from erp.hr.models import Dependent
    t = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t.id, source_note="홈택스 원표 표본 10칸 대조 일치")
    r = await _rec(session, "RF-1", tax_basis={"method": "근사산식", "dependents": 1, "taxable": 3_005_000})
    session.add_all([Dependent(employee_id=r.employee_id, name="배우자", relation="SPOUSE", counted=True),
                     Dependent(employee_id=r.employee_id, name="자녀", relation="CHILD", birth_date=date(2015, 5, 1),
                               counted=True)])
    await session.flush()
    row = (await wht.retax_preview(session, table_id=t.id))["rows"][0]
    assert (row["family"], row["recorded_family"], row["children"]) == (3, 1, 1)
    exp = wht.compute(T, 3_005_000, family=3, children=1)
    assert row["new"] == exp["소득세"] + exp["지방소득세"]
    await wht.retax(session, actor=1, table_id=t.id, note="정정 전 반영")
    # 같은 시행일 정정본 활성화 → 옛 표(RETIRED) id로 계산된 줄이 다시 대상
    t2 = await _candidate(session)
    await wht.activate(session, actor=1, table_id=t2.id, source_note="재대조 — 정정본 활성화")
    assert (await wht.retax_preview(session, table_id=t2.id))["targets"] == 1
