"""지출·경비 관리 (Tier1·마이그 0210) — 경비청구 생애주기·법인카드 적재/연결/월정산·회계 검증."""
from datetime import date

import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.expense_service import ExpenseService
from erp.finance.models import Account, CardTransaction, Voucher, VoucherLine

svc = ExpenseService()
D = date(2026, 7, 15)


async def _seed_accounts(session):
    session.add_all([
        Account(code="5340", name="여비교통비", kind="EXPENSE"),
        Account(code="5350", name="접대비", kind="EXPENSE"),
    ])
    await session.flush()


async def _lines_of(session, ref):
    v = await session.scalar(select(Voucher).where(Voucher.source_ref == ref))
    assert v is not None, f"전표 없음: {ref}"
    rows = (await session.scalars(select(VoucherLine).where(
        VoucherLine.voucher_id == v.id))).all()
    return {(ln.account_code, ln.side, float(ln.amount)) for ln in rows}


async def test_card_endpoints_row_scope(session, client):
    """v1.203 — 카드 목록/거래 행스코프: SELF는 본인 보유+본부서 공용만. 타 카드 거래는 403.

    적대검토 발견: read 게이트만 통과시키고 SELF/DEPT kind를 폐기해 전 직원급이 전 카드·
    거래(가맹점·금액)를 열람하던 결함."""
    from erp.finance.models import CorporateCard
    from erp.hr.models import Employee
    from tests._http import auth_as

    session.add(Employee(id=77, employee_no="E077", name="나직원", department_id=5,
                         hired_on=date(2025, 1, 1)))
    await session.flush()
    session.add_all([
        CorporateCard(id=1, alias="내카드", card_last4="1111", holder_employee_id=77,
                      department_id=5),
        CorporateCard(id=2, alias="부서공용", card_last4="2222", holder_employee_id=None,
                      department_id=5),
        CorporateCard(id=3, alias="타부서임원", card_last4="3333", holder_employee_id=99,
                      department_id=8),
    ])
    # 카드 선-flush — relationship 미정의 매퍼 간 INSERT 순서는 FK만으론 미보장(PG FK 강제 적발·
    # 실사용도 카드 생성→거래 적재 분리). sqlite는 FK 미강제라 잠복이던 갭.
    await session.flush()
    session.add(CardTransaction(id=10, card_id=3, tx_id="TX-1", tx_on=D, amount=100000,
                                merchant="룸살롱", status="UNMATCHED"))
    await session.flush()
    # STAFF(SELF 스코프) — user.employee_id=77 연결
    headers = await auth_as(session, user_id=50, role="STAFF", dept_id=5)
    from erp.common.models import User
    (await session.get(User, 50)).employee_id = 77
    await session.flush()
    r = await client.get("/api/v1/finance/corporate-cards", headers=headers)
    assert r.status_code == 200
    ids = {c["id"] for c in r.json()["items"]}
    assert ids == {1, 2}                       # 본인 보유+본부서 공용만(타부서 임원카드 제외)
    r = await client.get("/api/v1/finance/corporate-cards/3/transactions", headers=headers)
    assert r.status_code == 403                # 스코프 밖 카드 거래 열람 차단
    r = await client.get("/api/v1/finance/corporate-cards/1/transactions", headers=headers)
    assert r.status_code == 200


async def test_proxy_claim_uses_target_employee_dept(session, client):
    """v1.204 — ALL 스코프 대리 기안 시 department_id는 명의자(직원) 부서(관리자 부서 아님).

    경계(finance→hr 금지): 명의자 부서는 common.User(employee_id 연결)로 해석."""
    from tests._http import auth_as, make_user

    await _seed_accounts(session)
    await make_user(session, user_id=60, role="STAFF", dept_id=7)   # 명의자 계정=부서7
    from erp.common.models import User
    (await session.get(User, 60)).employee_id = 88
    await session.flush()
    headers = await auth_as(session, user_id=1, role="ADMIN", dept_id=2)   # 관리자=부서2
    r = await client.post("/api/v1/finance/expenses", headers=headers, json={
        "title": "대리 기안", "claim_on": "2026-07-15", "employee_id": 88,
        "lines": [{"expense_on": "2026-07-10", "account_code": "5340",
                   "description": "출장", "amount": 10000}]})
    assert r.status_code == 201, r.text
    from erp.finance.models import ExpenseClaim
    c = await session.get(ExpenseClaim, r.json()["id"])
    assert c.employee_id == 88 and c.department_id == 7    # 명의자 부서(2 아님)


async def test_doc_no_lock_helper_noop_on_sqlite(session):
    """v1.204 — 채번 직렬화 헬퍼: sqlite no-op(예외 없음)·PG에서만 advisory lock."""
    from erp.common.doc_no import lock_doc_no
    await lock_doc_no(session, "구매요청-2026")   # 예외 없이 통과


async def test_validation_422_error_envelope(session, client):
    """v1.203 — pydantic 422도 {"error":{code,message}} 표준 포맷(FE 토스트 노출용)."""
    from tests._http import auth_as
    headers = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/finance/expenses", headers=headers,
                          json={"title": "x"})   # lines·claim_on 누락
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "VALIDATION-422"
    assert "입력값 오류" in body["error"]["message"]


async def test_claim_lifecycle_personal_expense(session):
    """개인 경비 — 기안→상신→승인(DR비용/CR2110)→지급(DR2110/CR1110)·SoD."""
    await _seed_accounts(session)
    c = await svc.create_claim(
        session, employee_id=10, department_id=3, title="7월 출장", claim_on=D,
        created_by=100,
        lines=[{"expense_on": D, "account_code": "5340", "description": "KTX",
                "amount": 50_000},
               {"expense_on": D, "account_code": "5350", "description": "거래처 식사",
                "amount": 30_000}])
    assert c.status == "DRAFT" and float(c.total_amount) == 80_000
    await svc.submit(session, claim_id=c.id, actor=100)
    assert c.status == "PENDING"
    # SoD — 기안자 본인 승인 차단(룰 없음=단일승인 폴백)
    with pytest.raises(ValidationError):
        await svc.approve(session, claim_id=c.id, actor=100)
    out = await svc.approve(session, claim_id=c.id, actor=200)
    assert out.status == "APPROVED"
    assert await _lines_of(session, f"expense:{c.id}") == {
        ("5340", "DEBIT", 50_000), ("5350", "DEBIT", 30_000), ("2110", "CREDIT", 80_000)}
    pay = await svc.pay(session, claim_id=c.id, paid_on=date(2026, 7, 25), actor=200)
    assert pay["paid"] == 80_000 and c.status == "PAID"
    assert await _lines_of(session, f"expense-pay:{c.id}") == {
        ("2110", "DEBIT", 80_000), ("1110", "CREDIT", 80_000)}


async def test_non_expense_account_rejected(session):
    """비용(EXPENSE) 아닌 계정 라인 차단."""
    await _seed_accounts(session)
    session.add(Account(code="1110", name="현금", kind="ASSET"))
    await session.flush()
    with pytest.raises(ValidationError):
        await svc.create_claim(
            session, employee_id=10, department_id=3, title="x", claim_on=D, created_by=1,
            lines=[{"expense_on": D, "account_code": "1110", "description": "현금계정",
                    "amount": 1_000}])


async def test_card_flow_import_link_settle(session):
    """법인카드 — 멱등 적재→경비 연결(CLAIMED·금액일치 강제)→승인(CR2112)→월정산(DR2112/CR1110·SETTLED)."""
    await _seed_accounts(session)
    card = await svc.register_card(session, alias="법인카드-원무", card_last4="1234")
    out = await svc.import_transactions(session, card_id=card.id, rows=[
        {"tx_id": "APV-1", "tx_on": D, "amount": 40_000, "merchant": "주유소"},
        {"tx_id": "APV-2", "tx_on": D, "amount": 60_000, "merchant": "식당"}])
    assert out["added"] == 2
    # 재적재 멱등
    again = await svc.import_transactions(session, card_id=card.id, rows=[
        {"tx_id": "APV-1", "tx_on": D, "amount": 40_000}])
    assert again["added"] == 0 and again["skipped"] == 1
    tx1 = await session.scalar(select(CardTransaction).where(CardTransaction.tx_id == "APV-1"))
    # 금액 불일치 라인 차단
    with pytest.raises(ValidationError):
        await svc.create_claim(
            session, employee_id=10, department_id=3, title="카드경비", claim_on=D,
            created_by=100,
            lines=[{"expense_on": D, "account_code": "5340", "description": "주유",
                    "amount": 39_999, "card_tx_id": tx1.id}])
    c = await svc.create_claim(
        session, employee_id=10, department_id=3, title="카드경비", claim_on=D,
        created_by=100,
        lines=[{"expense_on": D, "account_code": "5340", "description": "주유",
                "amount": 40_000, "card_tx_id": tx1.id},
               {"expense_on": D, "account_code": "5350", "description": "개인 식대",
                "amount": 10_000}])
    assert tx1.status == "CLAIMED" and tx1.claim_id == c.id
    await svc.submit(session, claim_id=c.id, actor=100)
    await svc.approve(session, claim_id=c.id, actor=200)
    # 혼합: 카드분 2112·개인분 2110 분리
    assert await _lines_of(session, f"expense:{c.id}") == {
        ("5340", "DEBIT", 40_000), ("5350", "DEBIT", 10_000),
        ("2112", "CREDIT", 40_000), ("2110", "CREDIT", 10_000)}
    # 월정산 — 승인된 청구 귀속 CLAIMED만(미연결 APV-2 제외)
    st = await svc.settle_card_month(session, card_id=card.id, yyyymm="2026-07",
                                     paid_on=date(2026, 8, 5), actor=1)
    assert st["settled"] == 40_000 and st["count"] == 1
    assert tx1.status == "SETTLED"
    assert await _lines_of(session, st["voucher_ref"]) == {
        ("2112", "DEBIT", 40_000), ("1110", "CREDIT", 40_000)}
    # 재정산 — 잔여 0
    st2 = await svc.settle_card_month(session, card_id=card.id, yyyymm="2026-07",
                                      paid_on=date(2026, 8, 6), actor=1)
    assert st2["settled"] == 0


async def test_reject_releases_card_tx(session):
    """반려 시 연결 카드내역 UNMATCHED 원복(재청구 가능)."""
    await _seed_accounts(session)
    card = await svc.register_card(session, alias="법인카드-2", card_last4="9999")
    await svc.import_transactions(session, card_id=card.id, rows=[
        {"tx_id": "APV-9", "tx_on": D, "amount": 20_000}])
    tx = await session.scalar(select(CardTransaction).where(CardTransaction.tx_id == "APV-9"))
    c = await svc.create_claim(
        session, employee_id=11, department_id=3, title="반려건", claim_on=D, created_by=100,
        lines=[{"expense_on": D, "account_code": "5340", "description": "택시",
                "amount": 20_000, "card_tx_id": tx.id}])
    await svc.submit(session, claim_id=c.id, actor=100)
    await svc.reject(session, claim_id=c.id, actor=200, reason="증빙 미비")
    assert c.status == "REJECTED"
    assert tx.status == "UNMATCHED" and tx.claim_id is None


async def test_expense_attachment_passes_check_constraint(session):
    """경비 영수증 첨부가 ck_attachment_entity 통과 — v1.175 제약 누락(적대검토 H-2) 회귀."""
    from erp.common.models import Attachment
    session.add(Attachment(entity_type="expense", entity_id=1, filename="receipt.jpg",
                           content_type="image/jpeg", size_bytes=100, sha256="x" * 64,
                           uploaded_by=1))
    await session.flush()   # CHECK 위반이면 IntegrityError


async def test_pay_card_only_no_cash_voucher(session):
    """전액 카드 청구 — 지급 시 개인분 0이라 현금전표 없이 PAID(카드분은 월정산)."""
    await _seed_accounts(session)
    card = await svc.register_card(session, alias="법인카드-3", card_last4="0001")
    await svc.import_transactions(session, card_id=card.id, rows=[
        {"tx_id": "APV-C1", "tx_on": D, "amount": 15_000}])
    tx = await session.scalar(select(CardTransaction).where(CardTransaction.tx_id == "APV-C1"))
    c = await svc.create_claim(
        session, employee_id=12, department_id=3, title="카드전액", claim_on=D, created_by=100,
        lines=[{"expense_on": D, "account_code": "5340", "description": "숙박",
                "amount": 15_000, "card_tx_id": tx.id}])
    await svc.submit(session, claim_id=c.id, actor=100)
    await svc.approve(session, claim_id=c.id, actor=200)
    pay = await svc.pay(session, claim_id=c.id, paid_on=date(2026, 7, 30), actor=200)
    assert pay["paid"] == 0 and pay["card_deferred"] == 15_000 and c.status == "PAID"
    assert await session.scalar(select(Voucher).where(
        Voucher.source_ref == f"expense-pay:{c.id}")) is None
