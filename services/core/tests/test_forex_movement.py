"""외화 Stage2 — 입출금 통화별 계좌 라우팅 + 기말환산 연계."""
from datetime import date

import pytest
from sqlalchemy import func, select

from erp.errors import ValidationError
from erp.finance.forex_service import ForexTranslationService
from erp.finance.models import CorporateAccount, VoucherLine

svc = ForexTranslationService()


async def _acct(session, currency="USD", gl="1115"):
    a = CorporateAccount(alias="USD", bank_name="신한", account_no=f"X-{currency}",
                         currency=currency, gl_account_code=gl)
    session.add(a)
    await session.flush()
    return a


async def _gl(session, code):
    d, c = (await session.execute(select(
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "DEBIT"), 0),
        func.coalesce(func.sum(VoucherLine.amount).filter(VoucherLine.side == "CREDIT"), 0))
        .where(VoucherLine.account_code == code))).one()
    return float(d) - float(c)


async def test_fx_deposit_routes_to_currency_account(session):
    a = await _acct(session)
    r = await svc.record_movement(session, account_id=a.id, direction="IN", fc_amount=100_000,
                                  rate=1_200, on=date(2026, 1, 1))
    assert r["krw"] == 120_000_000
    assert await _gl(session, "1115") == 120_000_000     # 외화예금 차변
    assert await _gl(session, "1110") == -120_000_000    # 현금 대변
    # 기말환산 — @1,300 → 환산이익 10,000,000
    res = await svc.post(session, account_id=a.id, fc_balance=100_000, rate=1_300, yyyymm="2026-06")
    assert res["posted"] and res["delta"] == 10_000_000


async def test_fx_withdraw_reverse(session):
    a = await _acct(session, currency="EUR", gl="1115")
    await svc.record_movement(session, account_id=a.id, direction="OUT", fc_amount=1_000,
                              rate=1_400, on=date(2026, 2, 1))
    assert await _gl(session, "1115") == -1_400_000      # 외화예금 감소(대변)


async def test_fx_krw_account_rejected(session):
    a = await _acct(session, currency="KRW", gl="1110")
    with pytest.raises(ValidationError, match="원화"):
        await svc.record_movement(session, account_id=a.id, direction="IN", fc_amount=100,
                                  rate=1, on=date(2026, 1, 1))


# ---- 화면이 쓰는 계약(v1.278) ----

async def test_preview_returns_fields_screen_needs(session):
    """화면이 '미리보기 → 확인 → 전기'로 동작하려면 preview가 이 필드들을 줘야 한다.

    종전 화면은 prompt 3연타로 곧바로 전기했다 — 얼마가 전기될지 못 보고 GL을 확정했고
    서버 경고(동일 통화 다계좌 합계 안내)도 볼 수 없었다.
    """
    acct = await _acct(session)
    out = await svc.preview(session, account_id=acct.id, fc_balance=10000,
                            rate=1300, yyyymm="2026-03")
    for k in ("currency", "gl", "translated_krw", "book_value", "delta"):
        assert k in out, k
    assert out["translated_krw"] == 13_000_000


async def test_preview_warns_on_shared_gl_same_currency(session):
    """동일 통화 다계좌 공유 시 경고를 실어 보낸다 — 화면이 그대로 노출한다."""
    acct = await _acct(session)
    session.add(CorporateAccount(alias="국민 USD", bank_name="국민", account_no="9-USD",
                                 currency="USD", gl_account_code=acct.gl_account_code))
    await session.flush()
    out = await svc.preview(session, account_id=acct.id, fc_balance=10000,
                            rate=1300, yyyymm="2026-03")
    assert out["warning"] and "합계" in out["warning"]
    assert out["shared_gl_accounts"]
