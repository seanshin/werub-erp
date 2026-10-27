"""법정 파라미터 관리화면 등록 — 2027-01-01 원천징수 정지 예방(코드 배포 없이 고시값 등록).

달력 독립: 고정 연도(2031)를 쓴다 — 코드 상수에 없는 미래 연도.
"""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.hr import statutory
from erp.hr.models import Employee
from erp.hr.statutory_params import carry_tax, registry
from erp.hr.statutory_params import set_insurance as _set_insurance


async def set_insurance(session, **kw):
    """서비스 + 커밋 후 갱신(엔드포인트 _commit_then_refresh와 동일 효과)."""
    row = await _set_insurance(session, **kw)
    await statutory.refresh_db_params(session)
    return row

Y = 2031
VALS = {"pension": 0.05, "pension_min": 410000, "pension_max": 6500000, "health": 0.036,
        "longterm_of_health": 0.131, "employment": 0.009}


@pytest.fixture(autouse=True)
def _clean_cache():
    yield
    statutory._DB_INSURANCE.clear()
    statutory._DB_TAX_CARRY.clear()
    statutory._DB_TAX_AMEND.clear()
    statutory._TAX_AMEND_RESOLVED.clear()


async def test_unregistered_year_fails_safe_with_ui_guidance(session):
    await statutory.refresh_db_params(session)
    with pytest.raises(ValidationError, match="/admin/payroll-settings"):
        statutory.insurance(3_000_000, year=Y)
    with pytest.raises(ValidationError, match="전년 승계"):
        statutory.estimate_monthly_income_tax(3_000_000, year=Y)


async def test_registered_insurance_used_in_calculation(session):
    await set_insurance(session, year=Y, values=VALS, source_note="보건복지부 고시 제2030-1호", actor=1)
    out = statutory.insurance(3_000_000, year=Y)
    assert out["국민연금"] == 150_000                     # 3,000,000 × 5%
    assert out["건강보험"] == 108_000


async def test_db_overrides_code_year(session):
    """코드에 있는 연도(2026)도 관리화면 등록이 우선 — 고시 대조 후 정정 경로."""
    code = statutory.insurance(3_000_000, year=2026)["국민연금"]
    await set_insurance(session, year=2026, values={**VALS, "pension": 0.05},
                        source_note="정정 테스트", actor=1)
    assert statutory.insurance(3_000_000, year=2026)["국민연금"] == 150_000 != code


async def test_validation_catches_percent_typos(session):
    with pytest.raises(ValidationError, match="0.0475"):
        await set_insurance(session, year=Y, values={**VALS, "pension": 4.75},
                            source_note="고시", actor=1)
    with pytest.raises(ValidationError, match="누락"):
        await set_insurance(session, year=Y, values={"pension": 0.05}, source_note="고시", actor=1)
    with pytest.raises(ValidationError, match="근거"):
        await set_insurance(session, year=Y, values=VALS, source_note="", actor=1)
    with pytest.raises(ValidationError, match="하한"):
        await set_insurance(session, year=Y, values={**VALS, "pension_min": 7_000_000},
                            source_note="고시", actor=1)


async def test_tax_carry_over(session):
    base = statutory.estimate_monthly_income_tax(4_000_000, year=2026)
    await carry_tax(session, year=Y, from_year=2026, source_note="세법 개정 없음 확인", actor=1)
    await statutory.refresh_db_params(session)
    assert statutory.estimate_monthly_income_tax(4_000_000, year=Y) == base
    with pytest.raises(ValidationError):
        await carry_tax(session, year=2026, from_year=2025, source_note="x" * 5, actor=1)  # 코드 등록 연도


async def test_registry_reports_sources(session):
    from datetime import date
    await set_insurance(session, year=Y, values=VALS, source_note="고시 2030-1", actor=1)
    reg = await registry(session, today=date(Y - 1, 10, 1))
    by = {r["year"]: r for r in reg["years"]}
    assert by[Y]["insurance_source"] == "db" and by[2026]["insurance_source"] == "code"
    assert reg["next_year"] == Y and reg["next_year_ready"]["insurance_rates"] is True
    assert reg["next_year_ready"]["tax_params"] is False


async def test_endpoints_registered_and_rbac(session, client):
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.put(f"/api/v1/hr/statutory-params/{Y}/insurance", headers=admin,
                         json={"values": VALS, "source_note": "고시 2030-1"})
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/hr/statutory-params/{Y}/tax-carry", headers=admin,
                          json={"from_year": 2026, "source_note": "개정 없음 확인"})
    assert r.status_code == 200, r.text
    g = await client.get("/api/v1/hr/statutory-params", headers=admin)
    assert g.status_code == 200 and any(y["year"] == Y for y in g.json()["years"])
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.put(f"/api/v1/hr/statutory-params/{Y}/insurance", headers=staff,
                             json={"values": VALS, "source_note": "고시"})).status_code == 403


async def test_session_hook_refreshes_only_when_stale(session, monkeypatch):
    """F1·위험-1 — 훅은 TTL 안이면 DB를 건드리지 않고, 지나면 갱신한다(get_session 경로 직접 검증)."""
    from erp import db
    calls = []

    async def fake_refresh(s):
        calls.append(1)
    hooks = [(statutory._stale, fake_refresh)]
    monkeypatch.setattr(db, "_session_hooks", hooks)

    class _SF:
        def __call__(self):
            return session_ctx()

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def session_ctx():
        yield session
    monkeypatch.setattr(db, "session_factory", lambda: _SF())
    monkeypatch.setattr(session, "begin", lambda: _null())

    @asynccontextmanager
    async def _null():
        yield
    statutory.invalidate_cache()
    statutory._last_refresh[0] = float("-inf")
    gen = db.get_session()
    await gen.__anext__()
    assert calls == [1]                       # 만료 → 갱신
    statutory._last_refresh[0] = __import__("time").monotonic()
    gen2 = db.get_session()
    await gen2.__anext__()
    assert calls == [1]                       # TTL 안 → 호출 안 함(SAVEPOINT도 안 연다)


async def test_endpoint_reports_unconfirmed_payrolls(session, client):
    from erp.hr.models import PayrollRecord
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    e = Employee(employee_no="P-1", name="p", department_id=1, hired_on=__import__("datetime").date(2020, 1, 1),
                 job_group="NURSE")
    session.add(e)
    await session.flush()
    session.add(PayrollRecord(employee_id=e.id, yyyymm=f"{Y}-01"))
    await session.flush()
    r = await client.put(f"/api/v1/hr/statutory-params/{Y}/insurance", headers=admin,
                         json={"values": VALS, "source_note": "고시 2030-1"})
    assert r.status_code == 200 and r.json()["unconfirmed_payrolls"] == 1


@pytest.fixture
async def client_real_tx(session):
    """실제 get_session처럼 `async with session.begin():` 안에서 yield — 커밋 후 DB 조작을
    하면 prod에서만 터지는 오류(InvalidRequestError: closed transaction)를 잡는다(2차 검토 H4).
    conftest client는 begin 컨텍스트가 없어 이 결함을 가렸다. PG 레인은 외부 트랜잭션 공유라 sqlite만."""
    import os

    from httpx import ASGITransport, AsyncClient

    from erp.db import get_session
    from erp.main import create_app
    if os.environ.get("TEST_DATABASE_URL"):
        pytest.skip("sqlite 전용(PG 레인은 외부 트랜잭션 공유)")
    app = create_app()

    async def _real_like():
        async with session.begin():
            yield session

    app.dependency_overrides[get_session] = _real_like
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_statutory_endpoints_under_real_transaction(session, client_real_tx):
    from erp.common.security import create_access_token
    from erp.common.models import User
    session.add(User(id=1, email="u1@test.example", role="ADMIN", password_hash="x", token_version=0,
                     is_active=True))
    await session.commit()
    h = {"Authorization": "Bearer " + create_access_token(user_id=1, role="ADMIN", dept_id=None,
                                                         token_version=0, license_flag=True)}
    r = await client_real_tx.put(f"/api/v1/hr/statutory-params/{Y}/insurance", headers=h,
                                 json={"values": VALS, "source_note": "고시 2030-1"})
    assert r.status_code == 200, r.text
    r = await client_real_tx.post(f"/api/v1/hr/statutory-params/{Y}/tax-carry", headers=h,
                                  json={"from_year": 2026, "source_note": "개정 없음 확인"})
    assert r.status_code == 200, r.text


async def test_h2_pension_bounds_optional_pair(session):
    """7월분부터의 상·하한 — 둘 다 넣거나 둘 다 비움. 넣으면 7월분부터 적용."""
    with pytest.raises(ValidationError, match="둘 다"):
        await set_insurance(session, year=Y, values={**VALS, "pension_min_h2": 410_000},
                            source_note="공단 고시 2026", actor=1)
    with pytest.raises(ValidationError, match="7월분"):
        await set_insurance(session, year=Y, values={**VALS, "pension_min_h2": 9_000_000,
                                                     "pension_max_h2": 6_590_000},
                            source_note="공단 고시 2026", actor=1)
    await set_insurance(session, year=Y, values={**VALS, "pension_min_h2": 410_000,
                                                 "pension_max_h2": 9_000_000},
                        source_note="공단 고시 2026", actor=1)
    await statutory.refresh_db_params(session)
    r6 = statutory.insurance(20_000_000, year=Y, month=6)["국민연금"]
    r7 = statutory.insurance(20_000_000, year=Y, month=7)["국민연금"]
    assert r7 > r6


async def test_rate_diff_reports_underdeduction_without_changing_payroll(client, session):
    """2025 요율로 생성·확정된 2026-07 급여 ↔ 고시 요율 재계산 차액 — 급여 라인은 그대로."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select

    from erp.hr.models import Employee, PayrollLine, PayrollRecord
    from tests._http import auth_as
    admin = await auth_as(session, user_id=701, role="ADMIN")
    staff = await auth_as(session, user_id=702, role="STAFF")
    e = Employee(employee_no="RD-1", name="차액", hired_on=date(2020, 1, 1), employment_type="REGULAR")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-07", confirmed_at=datetime.now(UTC),
                        paid_on=date(2026, 7, 25))
    session.add(rec)
    await session.flush()
    for code, amt, basis in (("NP", 135_000, {"base": 3_000_000, "rate": 0.045}),
                             ("HI", 106_350, {"base": 3_000_000, "rate": 0.03545}),
                             ("LTC", 13_770, {"base": 106_350, "rate": 0.1295}),
                             ("EI", 27_000, {"base": 3_000_000, "rate": 0.009})):
        session.add(PayrollLine(record_id=rec.id, item_code=code, item_kind="DEDUCTION", amount=amt,
                                calc_basis=basis))
    await session.flush()
    url = "/api/v1/hr/statutory-params/rate-diff?yyyymm=2026-07"
    assert (await client.get(url, headers=staff)).status_code == 403
    assert (await client.get("/api/v1/hr/statutory-params/rate-diff?yyyymm=2026-7",
                             headers=admin)).status_code == 422
    d = (await client.get(url, headers=admin)).json()
    assert d["records"] == 1 and d["affected"] == 1
    assert d["rows"][0]["diff"] == {"NP": 7_500, "HI": 1_500, "LTC": 400}       # EI 동일
    assert d["employee_diff_total"] == 9_400
    assert d["totals"]["NP"]["recorded"] == 135_000 and d["totals"]["NP"]["recalculated"] == 142_500
    ln = await session.scalar(select(PayrollLine).where(PayrollLine.record_id == rec.id,
                                                         PayrollLine.item_code == "NP"))
    assert int(ln.amount) == 135_000                                            # 급여는 그대로


def test_month_of_accepts_compact_form():
    """라우터는 "202610"도 받는다 — 슬라이스 [5:7]은 압축형에서 10~12월을 0·1·2로 읽어 하반기 상·하한을 놓쳤다."""
    assert statutory.month_of("2026-10") == 10 and statutory.month_of("202610") == 10
    assert statutory.month_of("202607") == 7 and statutory.month_of("2026-01") == 1


async def test_filing_uses_recorded_deductions_and_rate_diff_logs_access(client, session):
    """신고 집계는 실제 공제 라인을 원천으로(요율 정정 후 재계산하면 공제·전기와 갈림) · 차액 조회는 접속기록."""
    from datetime import UTC, date, datetime

    from sqlalchemy import select

    from erp.audit.models import AccessLog
    from erp.hr.models import Employee, PayrollLine, PayrollRecord
    from erp.tax.service import TaxService
    from tests._http import auth_as
    admin = await auth_as(session, user_id=703, role="ADMIN")
    e = Employee(employee_no="RD-2", name="신고", hired_on=date(2020, 1, 1), employment_type="REGULAR")
    session.add(e)
    await session.flush()
    rec = PayrollRecord(employee_id=e.id, yyyymm="2026-07", confirmed_at=datetime.now(UTC))
    session.add(rec)
    await session.flush()
    session.add_all([
        PayrollLine(record_id=rec.id, item_code="BASE", item_kind="EARNING", amount=3_000_000),
        PayrollLine(record_id=rec.id, item_code="NP", item_kind="DEDUCTION", amount=135_000,
                    calc_basis={"base": 3_000_000, "rate": 0.045}),
        PayrollLine(record_id=rec.id, item_code="HI", item_kind="DEDUCTION", amount=106_350,
                    calc_basis={"base": 3_000_000, "rate": 0.03545}),
        PayrollLine(record_id=rec.id, item_code="LTC", item_kind="DEDUCTION", amount=13_770,
                    calc_basis={"base": 106_350}),
        PayrollLine(record_id=rec.id, item_code="EI", item_kind="DEDUCTION", amount=27_000,
                    calc_basis={"base": 3_000_000})])
    await session.flush()
    f = await TaxService().insurance_filing(session, yyyymm="202607")
    assert f["근로자부담"]["국민연금"] == 135_000 and f["근로자부담"]["합계"] == 282_120   # 공제된 그대로
    assert f["산출근거"] == {"공제기록": 1, "현재요율_재계산": 0}
    d = (await client.get("/api/v1/hr/statutory-params/rate-diff?yyyymm=202607", headers=admin)).json()
    assert d["yyyymm"] == "2026-07" and d["records"] == 1 and d["employee_diff_total"] == 9_400
    logs = (await session.scalars(select(AccessLog).where(AccessLog.target_id == "rate-diff:2026-07"))).all()
    assert logs


async def test_employer_rates_per_year_override_settings(session):
    """사업주 고용·산재 요율 연도별 등록(v1.297) — 등록 연도는 그 값, 미등록 연도는 급여 설정 값."""
    await set_insurance(session, year=Y, values={**VALS, "employer_ei": 0.0125, "employer_wc": 0.0081},
                        source_note="고용노동부 고시 2031", actor=1)
    assert statutory.employer_rates(Y, fallback_ei=0.0115, fallback_wc=0.007)[:2] == (0.0125, 0.0081)
    assert statutory.employer_rates(Y - 1, fallback_ei=0.0115, fallback_wc=0.007)[:2] == (0.0115, 0.007)
    with pytest.raises(ValidationError, match="employer_wc"):
        await set_insurance(session, year=Y, values={**VALS, "employer_wc": 0.81}, source_note="오타", actor=1)


async def test_confirm_posts_employer_burden_with_year_rates(session):
    from erp.finance.models import Voucher, VoucherLine
    from erp.hr.employee_service import PayrollService
    from erp.hr.models import Employee, PayrollSettings
    await set_insurance(session, year=2026, values={
        "pension": 0.0475, "pension_min": 400000, "pension_max": 6370000, "health": 0.03595,
        "longterm_of_health": 0.1314, "employment": 0.009, "employer_ei": 0.0175, "employer_wc": 0.01},
        source_note="2026 고시 대조", actor=1)
    cfg = await session.get(PayrollSettings, 1) or PayrollSettings(id=1)
    cfg.employer_burden_posting, cfg.employer_ei_rate, cfg.employer_wc_rate = True, 0.0115, 0.007
    session.add(cfg)
    e = Employee(employee_no="ER-1", name="사업주", hired_on=date(2020, 1, 1), employment_type="REGULAR",
                 job_group="STAFF", source="ERP")
    session.add(e)
    await session.flush()
    pay = PayrollService()
    await pay.upsert_lines(session, actor=1, employee_id=e.id, yyyymm="2026-10",
                           lines=[{"item_code": "BASE", "item_kind": "EARNING", "amount": 3_000_000}])
    rec = await pay.confirm(session, actor=1, employee_id=e.id, yyyymm="2026-10")
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"payroll-employer:{rec.id}"))
    total = sum(int(x.amount) for x in await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id, VoucherLine.side == "DEBIT")))
    b = statutory.employer_burden(3_000_000, year=2026, month=10, ei_rate=0.0175, wc_rate=0.01)
    assert total == int(b["합계"])                          # 급여 설정(1.15%·0.7%)이 아니라 그 해 등록값



# ── v1.300 — 세법 개정 반영(코드 배포 없이 바뀐 항목만) ──────────────────────────────
async def test_tax_amend_overrides_only_changed_keys(session):
    from erp.hr.statutory_params import amend_tax
    br = statutory.tax_params_json(2026)["brackets"]
    br2 = [r[:] for r in br]
    br2[0][1] = 0.05                                      # 최저 구간 세율 개정 가정
    await amend_tax(session, year=Y, from_year=2026, overrides={"child_first": 300_000, "brackets": br2,
                                                                 "personal_deduction": 1_500_000},
                    source_note="2030 세법 개정(가정)", actor=1)
    await statutory.refresh_db_params(session)
    p = statutory._tax_params(Y)
    assert p["child_first"] == 300_000 and p["child_second"] == statutory._tax_params(2026)["child_second"]
    assert p["brackets"][0][1] == 0.05 and p["brackets"][-1][0] == float("inf")    # null → 무한대 복원
    from erp.hr.models import StatutoryParam
    row = await session.scalar(select(StatutoryParam).where(StatutoryParam.year == Y))
    assert sorted(row.values["overrides"]) == ["brackets", "child_first"]          # 원본과 같은 값은 저장 안 함
    assert Y in statutory.registered_years()["tax"]
    assert statutory._tax_params(2026)["child_first"] == 250_000                    # 원본은 그대로


@pytest.mark.parametrize("ov,msg", [
    ({"nope": 1}, "알 수 없는"), ({"child_first": -1}, "0 이상"), ({"daily_rate": 6}, "소수"),
    ({"daily_credit": 55}, "소수"), ({"brackets": [[14_000_000, 6, 0], [None, 0.45, 1]]}, "율"),
    ({"child_first": 250_000}, "원본과 같은"), ({}, "바뀐 항목이 없습니다"),
    ({"brackets": [[14_000_000, 0.06, 0], [None, 0.45, 1]]}, None),               # 모양은 맞음 — 통과
    ({"brackets": [[14_000_000, 0.06, 0], [5_000_000, 0.15, 1], [None, 0.45, 2]]}, "오름차순"),
    ({"brackets": [[14_000_000, 0.06, 0], [50_000_000, 0.45, 1]]}, "비워 두세요"),
    ({"brackets": [[14_000_000, 0.06]]}, "칸 수"),
])
async def test_tax_amend_validation(session, ov, msg):
    from erp.hr.statutory_params import amend_tax
    if msg is None:
        await amend_tax(session, year=Y, from_year=2026, overrides=ov, source_note="모양 검사", actor=1)
        return
    with pytest.raises(ValidationError, match=msg):
        await amend_tax(session, year=Y, from_year=2026, overrides=ov, source_note="모양 검사", actor=1)


async def test_carry_and_amend_replace_each_other(session, client):
    from erp.hr.models import StatutoryParam
    from tests._http import auth_as
    h = await auth_as(session, user_id=1, role="ADMIN")
    await carry_tax(session, year=Y, from_year=2026, source_note="개정 없음", actor=1)
    r = await client.post(f"/api/v1/hr/statutory-params/{Y}/tax-amend", headers=h,
                          json={"from_year": 2026, "overrides": {"child_first": 300_000}, "source_note": "개정 반영"})
    assert r.status_code == 200 and r.json()["overrides"] == ["child_first"]
    kinds = {k for (k,) in (await session.execute(select(StatutoryParam.kind).where(StatutoryParam.year == Y))).all()}
    assert kinds == {"TAX_AMEND"}
    await carry_tax(session, year=Y, from_year=2026, source_note="개정 철회 — 승계", actor=1)
    kinds = {k for (k,) in (await session.execute(select(StatutoryParam.kind).where(StatutoryParam.year == Y))).all()}
    assert kinds == {"TAX_CARRY"}
    g = await client.get("/api/v1/hr/statutory-params/tax/2026", headers=h)
    assert g.status_code == 200 and g.json()["params"]["brackets"][-1][0] is None
    staff = await auth_as(session, user_id=2, role="STAFF")
    assert (await client.post(f"/api/v1/hr/statutory-params/{Y}/tax-amend", headers=staff,
                              json={"from_year": 2026, "overrides": {"child_first": 1}, "source_note": "x"})).status_code == 403



async def test_tax_amend_merges_with_previous_amendment(session):
    """같은 연도 두 번째 개정은 먼저 넣은 항목을 유지한다 · 원본 값으로 되돌리면 개정에서 빠진다."""
    from erp.hr.models import StatutoryParam
    from erp.hr.statutory_params import amend_tax
    await amend_tax(session, year=Y, from_year=2026, overrides={"child_extra": 500_000}, source_note="1차 개정", actor=1)
    await amend_tax(session, year=Y, from_year=2026, overrides={"child_first": 300_000}, source_note="2차 개정", actor=1)
    row = await session.scalar(select(StatutoryParam).where(StatutoryParam.year == Y))
    assert row.values["overrides"] == {"child_extra": 500_000, "child_first": 300_000}
    await amend_tax(session, year=Y, from_year=2026, overrides={"child_extra": 400_000}, source_note="1차 철회", actor=1)
    row = await session.scalar(select(StatutoryParam).where(StatutoryParam.year == Y))
    assert row.values["overrides"] == {"child_first": 300_000}
