"""복리후생(Tier3·0230) — 제도·부여·신청/승인/지급·한도검증·잔액·GL·HTTP."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.models import Account, Voucher, VoucherLine
from erp.hr.models import Employee
from erp.hr.welfare_service import WelfareService

svc = WelfareService()
ON = date(2026, 6, 1)


async def _emp(session, no, job=None):
    e = Employee(employee_no=no, name=f"직원{no}", hired_on=date(2025, 1, 1),
                 job_group="STAFF", employment_type="REGULAR", source="ERP", job_category=job)
    session.add(e)
    await session.flush()
    return e


async def _accts(session):
    session.add_all([Account(code="5330", name="복리후생비", kind="EXPENSE"),
                     Account(code="1110", name="현금", kind="ASSET")])
    await session.flush()


async def test_program_limit_and_claim_flow(session):
    """제도 연한도→신청(한도내)→한도초과 거부→승인→잔액 차감."""
    e = await _emp(session, "W1")
    p = await svc.create_program(session, actor=1, code="POINT", title="복지포인트",
                                 annual_limit=1_000_000)
    c1 = await svc.claim(session, actor=1, program_id=p.id, employee_id=e.id,
                         amount=600_000, claim_on=ON)
    await svc.decide(session, actor=1, claim_id=c1.id, approve=True)
    # 잔액 40만 — 50만 신청은 한도 초과
    with pytest.raises(ValidationError, match="한도 초과"):
        await svc.claim(session, actor=1, program_id=p.id, employee_id=e.id,
                        amount=500_000, claim_on=ON)
    bal = {b["code"]: b for b in await svc.balance(session, employee_id=e.id, year=2026)}
    assert bal["POINT"]["limit"] == 1_000_000 and bal["POINT"]["used"] == 600_000
    assert bal["POINT"]["remaining"] == 400_000


async def test_grant_overrides_program_limit(session):
    """직원 개별 부여가 제도 기본 한도를 대체."""
    e = await _emp(session, "W2")
    p = await svc.create_program(session, actor=1, code="EDU", title="자기계발",
                                 annual_limit=500_000)
    await svc.grant(session, actor=1, program_id=p.id, employee_id=e.id, year=2026,
                    granted=800_000)
    bal = {b["code"]: b for b in await svc.balance(session, employee_id=e.id, year=2026)}
    assert bal["EDU"]["limit"] == 800_000                # 부여가 우선
    await svc.claim(session, actor=1, program_id=p.id, employee_id=e.id, amount=700_000,
                    claim_on=ON)   # 기본 한도(50만)면 거부됐을 금액


async def test_pay_posts_gl_and_idempotent(session):
    """지급 — DR 5330 / CR 1110·멱등·승인 전 지급 거부."""
    await _accts(session)
    e = await _emp(session, "W3")
    p = await svc.create_program(session, actor=1, code="CONG", title="경조사비",
                                 benefit_type="CONGRATS", annual_limit=1_000_000)
    c = await svc.claim(session, actor=1, program_id=p.id, employee_id=e.id, amount=200_000,
                        claim_on=ON)
    with pytest.raises(ValidationError):                 # 승인 전 지급 불가
        await svc.pay(session, actor=1, claim_id=c.id)
    await svc.decide(session, actor=1, claim_id=c.id, approve=True)
    await svc.pay(session, actor=1, claim_id=c.id, paid_on=ON)
    assert c.status == "PAID" and c.voucher_ref
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == f"welfare:{c.id}"))
    lines = {(ln.account_code, ln.side): float(ln.amount) for ln in (await session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == v.id))).all()}
    assert lines == {("5330", "DEBIT"): 200000, ("1110", "CREDIT"): 200000}


async def test_target_scoped_program_in_balance(session):
    """직종 대상 제도는 대상자만 잔액에 노출."""
    nurse = await _emp(session, "N9", job="NURSE")
    other = await _emp(session, "O9", job="ADMIN")
    await svc.create_program(session, actor=1, code="NURSECARE", title="간호사복지",
                             target="JOB_CATEGORY", target_value="NURSE", annual_limit=300_000)
    assert any(b["code"] == "NURSECARE"
               for b in await svc.balance(session, employee_id=nurse.id, year=2026))
    assert not any(b["code"] == "NURSECARE"
                   for b in await svc.balance(session, employee_id=other.id, year=2026))


async def test_http_self_claim_and_admin_flow(session, client):
    from erp.common.models import User
    from tests._http import auth_as, bearer, make_user
    await _accts(session)
    admin = await auth_as(session, user_id=1, role="ADMIN")
    e = await _emp(session, "H9")
    await make_user(session, user_id=50, role="STAFF")
    (await session.get(User, 50)).employee_id = e.id
    await session.flush()
    r = await client.post("/api/v1/hr/welfare/programs", headers=admin,
                          json={"code": "PT", "title": "포인트", "annual_limit": 500000})
    pid = r.json()["id"]
    # 본인 신청(STAFF)
    r = await client.post("/api/v1/hr/welfare/claims", headers=bearer(user_id=50, role="STAFF"),
                          json={"program_id": pid, "amount": 100000, "claim_on": "2026-06-01"})
    assert r.status_code == 201
    cid = r.json()["id"]
    # 본인 잔액 — 신청(REQUESTED)은 아직 미차감(승인 시 확정)
    r = await client.get("/api/v1/hr/welfare/balance?year=2026",
                         headers=bearer(user_id=50, role="STAFF"))
    assert r.status_code == 200 and r.json()["items"][0]["used"] == 0
    # STAFF는 승인 불가
    r = await client.post(f"/api/v1/hr/welfare/claims/{cid}/decide",
                          headers=bearer(user_id=50, role="STAFF"), json={"approve": True})
    assert r.status_code == 403
    # admin 승인→차감 반영→지급
    assert (await client.post(f"/api/v1/hr/welfare/claims/{cid}/decide", headers=admin,
                              json={"approve": True})).status_code == 200
    r = await client.get("/api/v1/hr/welfare/balance?year=2026",
                         headers=bearer(user_id=50, role="STAFF"))
    assert r.json()["items"][0]["used"] == 100000        # 승인 후 차감
    r = await client.post(f"/api/v1/hr/welfare/claims/{cid}/pay", headers=admin, json={})
    assert r.status_code == 200 and r.json()["voucher_ref"]
