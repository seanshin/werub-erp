"""법인 자금계좌 마스터 — CRUD·통화별 기본계좌 유일·GL 검증·계좌번호 마스킹."""
import pytest

from erp.common.router import CurrentUser
from erp.errors import ForbiddenError, ValidationError
from erp.finance.models import Account
from erp.finance.router import (
    CorpAccountBody, create_corp_account, list_corp_accounts,
)

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)
STAFF = CurrentUser(user_id=2, role="STAFF", dept_id=1, license_flag=True)


async def _seed_gl(session):
    session.add_all([Account(code="1110", name="현금성자산", kind="ASSET"),
                     Account(code="1115", name="외화예금", kind="ASSET")])
    await session.flush()


def _body(**kw):
    base = dict(alias="신한 USD", bank_name="신한", account_no="110-123-456789",
                currency="USD", gl_account_code="1115")
    base.update(kw)
    return CorpAccountBody(**base)


async def test_create_masks_account_no(session):
    await _seed_gl(session)
    out = await create_corp_account(_body(), user=ADMIN, session=session)
    assert out["account_no"].endswith("6789") and out["account_no"].startswith("*")  # 마스킹
    assert out["currency"] == "USD" and out["gl_account_code"] == "1115"


async def test_reveal_shows_full_number(session):
    await _seed_gl(session)
    await create_corp_account(_body(), user=ADMIN, session=session)
    masked = await list_corp_accounts(reveal=False, user=ADMIN, session=session)
    full = await list_corp_accounts(reveal=True, user=ADMIN, session=session)
    assert masked["items"][0]["account_no"] != "110-123-456789"
    assert full["items"][0]["account_no"] == "110-123-456789"


async def test_default_unique_per_currency(session):
    await _seed_gl(session)
    a = await create_corp_account(_body(account_no="A1", is_default=True), user=ADMIN, session=session)
    b = await create_corp_account(_body(account_no="A2", is_default=True), user=ADMIN, session=session)
    # 같은 통화(USD) 새 기본 지정 → 기존 기본 해제
    items = {x["id"]: x for x in (await list_corp_accounts(user=ADMIN, session=session))["items"]}
    assert items[a["id"]]["is_default"] is False and items[b["id"]]["is_default"] is True


async def test_invalid_currency_and_gl_rejected(session):
    await _seed_gl(session)
    with pytest.raises(ValidationError):
        await create_corp_account(_body(currency="XXX"), user=ADMIN, session=session)
    with pytest.raises(ValidationError):
        await create_corp_account(_body(gl_account_code="9999"), user=ADMIN, session=session)


async def test_non_admin_cannot_write(session):
    await _seed_gl(session)
    with pytest.raises(ForbiddenError):
        await create_corp_account(_body(), user=STAFF, session=session)


async def test_duplicate_bank_account_rejected(session):
    await _seed_gl(session)
    await create_corp_account(_body(account_no="DUP-1"), user=ADMIN, session=session)
    # 같은 은행+계좌번호 재등록 → 500(DB IntegrityError) 아닌 검증오류로 거부
    with pytest.raises(ValidationError):
        await create_corp_account(_body(account_no="DUP-1"), user=ADMIN, session=session)
