"""외화 기말환산 (K-IFRS 1021) — 외화잔액×기말환율 vs 장부가 환산차·멱등."""
from datetime import date

import pytest

from erp.finance.forex_service import ForexTranslationService
from erp.finance.models import Account, CorporateAccount
from erp.finance.service import VoucherLineDTO, VoucherService

svc = ForexTranslationService()
vs = VoucherService()


async def _seed(session):
    session.add_all([
        Account(code="1110", name="현금", kind="ASSET"),
        Account(code="1115", name="외화예금", kind="ASSET"),
        Account(code="4650", name="외화환산이익", kind="REVENUE"),
        Account(code="5670", name="외화환산손실", kind="EXPENSE"),
    ])
    acct = CorporateAccount(alias="신한 USD", bank_name="신한", account_no="110-USD",
                            currency="USD", gl_account_code="1115")
    session.add(acct)
    await session.flush()
    # 외화예금 장부가 12,000,000 (USD 10,000 @ 1,200 최초)
    await vs.create(session, source_module="manual", source_ref="fxopen",
                    voucher_on=date(2026, 1, 5),
                    lines=[VoucherLineDTO("1115", "DEBIT", 12000000),
                           VoucherLineDTO("1110", "CREDIT", 12000000)])
    return acct


async def test_fx_gain(session):
    acct = await _seed(session)
    # 기말 USD 10,000 @ 1,300 = 13,000,000 → 장부 12,000,000 → 환산이익 1,000,000
    p = await svc.preview(session, account_id=acct.id, fc_balance=10000, rate=1300, yyyymm="2026-06")
    assert p["translated_krw"] == 13000000 and p["book_value"] == 12000000 and p["delta"] == 1000000
    r = await svc.post(session, account_id=acct.id, fc_balance=10000, rate=1300, yyyymm="2026-06")
    assert r["posted"] and r["delta"] == 1000000
    # 재실행 — 장부 반영 후 환산차 0(멱등)
    r2 = await svc.post(session, account_id=acct.id, fc_balance=10000, rate=1300, yyyymm="2026-06")
    assert r2["posted"] is False


async def test_krw_account_rejected(session):
    session.add(Account(code="1110", name="현금", kind="ASSET"))
    a = CorporateAccount(alias="국민 원화", bank_name="국민", account_no="123", currency="KRW",
                         gl_account_code="1110")
    session.add(a)
    await session.flush()
    with pytest.raises(ValueError):
        await svc.preview(session, account_id=a.id, fc_balance=100, rate=1, yyyymm="2026-06")


async def test_mixed_currency_gl_blocks_translation(session):
    """다른 통화가 같은 GL을 공유하면 환산을 거부한다 — 조용히 틀린 환산손익을 막는다.

    `_book_value`는 GL 계정코드 전체를 합산하므로 통화가 섞이면 '이 계좌의 외화잔액×환율' vs
    '여러 통화 합산 장부가'를 비교하게 되어 환산차가 무의미해진다.
    prod에서 실제로 발현했다 — `fx-translate:8:2026-06`이 계좌 #8의 USD 잔액을 GL 1115 전체
    장부가(JPY·타 계좌·개설 전기 포함)와 비교해 환산손실 5,510,500을 전기했다(계좌가 전부
    TEST 데모라 실계정 피해는 없었다).
    """
    from erp.errors import ValidationError
    acct = await _seed(session)
    session.add(CorporateAccount(alias="국민 JPY", bank_name="국민", account_no="220-JPY",
                                 currency="JPY", gl_account_code="1115"))
    await session.flush()

    with pytest.raises(ValidationError, match="다른 통화 계좌가 공유"):
        await svc.preview(session, account_id=acct.id, fc_balance=10000,
                          rate=1300, yyyymm="2026-03")
    with pytest.raises(ValidationError, match="다른 통화 계좌가 공유"):
        await svc.post(session, account_id=acct.id, fc_balance=10000,
                       rate=1300, yyyymm="2026-03", actor=1)


async def test_same_currency_shared_gl_warns_not_blocks(session):
    """동일 통화 다계좌는 정상 실무 — 막지 않고, 장부가가 합산임을 경고로 알린다."""
    acct = await _seed(session)
    session.add(CorporateAccount(alias="국민 USD", bank_name="국민", account_no="220-USD",
                                 currency="USD", gl_account_code="1115"))
    await session.flush()
    out = await svc.preview(session, account_id=acct.id, fc_balance=10000,
                            rate=1300, yyyymm="2026-03")
    assert out["delta"] == 13000000 - 12000000
    assert "전체 외화잔액 합계" in out["warning"] and len(out["shared_gl_accounts"]) == 1


async def test_inactive_mixed_currency_gl_still_blocks(session):
    """⚠ 비활성 계좌도 막는다 — 계좌를 끄면 가드가 풀리는 우회를 차단한다.

    `_book_value`는 전기 이력 전체를 합산하므로 계좌를 비활성화해도 그 전기는 장부가에 남는다.
    처음엔 "폐쇄 계좌로 영구 차단되면 곤란하다"고 통과시켰는데, 그러면 통화가 섞인 장부가로
    환산차를 내는 원래 결함이 그대로 돌아온다. 해소는 비활성화가 아니라 전기 이관이다.
    """
    from erp.errors import ValidationError
    acct = await _seed(session)
    session.add(CorporateAccount(alias="폐쇄 JPY", bank_name="국민", account_no="220-JPY",
                                 currency="JPY", gl_account_code="1115", is_active=False))
    await session.flush()
    with pytest.raises(ValidationError, match="다른 통화 계좌가 공유"):
        await svc.preview(session, account_id=acct.id, fc_balance=10000,
                          rate=1300, yyyymm="2026-03")


async def test_corporate_account_create_rejects_mixed_currency_gl(session, client):
    """등록 시점 차단 — 다른 통화가 쓰는 GL로는 계좌를 만들 수 없다(동일 통화는 허용)."""
    from tests._http import auth_as
    await _seed(session)
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.post("/api/v1/finance/corporate-accounts", headers=admin, json={
        "alias": "국민 JPY", "bank_name": "국민", "account_no": "220-JPY",
        "currency": "JPY", "gl_account_code": "1115"})
    assert r.status_code == 422, r.text
    assert "다른 통화 계좌가 사용 중" in r.text
