"""계정과목(COA) 관리 — 생성·검증·수정·비활성·사용중 표시."""
import pytest
from sqlalchemy import select

from erp.errors import ValidationError
from erp.finance.account_service import AccountService
from erp.finance.models import Account
from erp.finance.service import VoucherLineDTO, VoucherService

svc = AccountService()


async def test_create_validates(session):
    a = await svc.create(session, code="6000", name="신규비용", kind="EXPENSE")
    assert a.code == "6000" and a.active is True
    with pytest.raises(ValidationError):                      # 중복 코드
        await svc.create(session, code="6000", name="중복", kind="EXPENSE")
    with pytest.raises(ValidationError):                      # 잘못된 kind
        await svc.create(session, code="6001", name="x", kind="WRONG")
    with pytest.raises(ValidationError):                      # 코드 누락
        await svc.create(session, code="  ", name="x", kind="ASSET")


async def test_update_name_kind_active(session):
    a = await svc.create(session, code="3500", name="자본금", kind="CAPITAL")
    await svc.update(session, account_id=a.id, name="자본금(수정)", active=False)
    got = await session.get(Account, a.id)
    assert got.name == "자본금(수정)" and got.active is False
    assert got.code == "3500"                                 # code 불변


async def test_kind_change_blocked_when_in_use(session):
    """심층 재검토 — 전기 내역 있는 계정의 종류 변경 차단(재무제표 소급 재분류 왜곡 방지)."""
    from datetime import date
    session.add_all([Account(code="1130", name="미수금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()
    await VoucherService().create(session, source_module="manual", source_ref="ku1",
                                  voucher_on=date(2026, 5, 1),
                                  lines=[VoucherLineDTO("1130", "DEBIT", 1000),
                                         VoucherLineDTO("4110", "CREDIT", 1000)])
    a = await session.scalar(select(Account).where(Account.code == "1130"))
    with pytest.raises(ValidationError, match="전기 내역"):
        await svc.update(session, account_id=a.id, kind="EXPENSE")   # 사용 중 → 차단
    # 같은 kind 재지정(변경 아님)·명칭 변경은 허용
    await svc.update(session, account_id=a.id, kind="ASSET", name="미수금(수정)")
    # 미사용 계정은 종류 변경 허용
    b = await svc.create(session, code="6100", name="임시", kind="EXPENSE")
    await svc.update(session, account_id=b.id, kind="REVENUE")
    assert (await session.get(Account, b.id)).kind == "REVENUE"


async def test_list_marks_in_use(session):
    from datetime import date
    session.add_all([Account(code="1110", name="현금", kind="ASSET"),
                     Account(code="4110", name="수익", kind="REVENUE")])
    await session.flush()
    await VoucherService().create(session, source_module="manual", source_ref="t1",
                                  voucher_on=date(2026, 5, 1),
                                  lines=[VoucherLineDTO("1110", "DEBIT", 1000),
                                         VoucherLineDTO("4110", "CREDIT", 1000)])
    items = {i["code"]: i for i in await svc.list(session)}
    assert items["1110"]["in_use"] is True and items["1110"]["kind_kr"] == "자산"
    # 미사용 신규 계정
    await svc.create(session, code="5999", name="기타비용", kind="EXPENSE")
    assert (await svc.list(session))[-1]["code"] in {"5999", "4110", "1110"}
    assert {i["code"]: i["in_use"] for i in await svc.list(session)}["5999"] is False


async def test_list_kind_filter_and_inactive(session):
    a = await svc.create(session, code="2222", name="부채A", kind="LIABILITY")
    await svc.update(session, account_id=a.id, active=False)
    assert [i["code"] for i in await svc.list(session, kind="LIABILITY")] == ["2222"]
    assert await svc.list(session, kind="LIABILITY", include_inactive=False) == []


async def test_cost_type_and_fixed_codes(session):
    a = await svc.create(session, code="5310", name="급여비용", kind="EXPENSE", cost_type="FIXED")
    b = await svc.create(session, code="5410", name="재료비", kind="EXPENSE", cost_type="VARIABLE")
    assert a.cost_type == "FIXED"
    with pytest.raises(ValidationError):
        await svc.create(session, code="5999", name="x", kind="EXPENSE", cost_type="WRONG")
    assert await svc.fixed_cost_codes(session) == {"5310"}
    await svc.update(session, account_id=b.id, cost_type="FIXED")
    assert await svc.fixed_cost_codes(session) == {"5310", "5410"}
    await svc.update(session, account_id=b.id, clear_cost_type=True)
    got = {i["code"]: i["cost_type"] for i in await svc.list(session)}
    assert got["5410"] is None and got["5310"] == "FIXED"
    assert [i["code"] for i in await svc.list(session, cost_type="FIXED")] == ["5310"]


async def test_ifrs_element_current_and_cf_activity(session):
    """G8 — 계정 IFRS element(유동/현금흐름) 설정·조회·검증·해제."""
    a = await svc.create(session, code="1130", name="미수금", kind="ASSET",
                         is_current=True, cf_activity="OPERATING")
    assert a.is_current is True and a.cf_activity == "OPERATING"
    got = {i["code"]: i for i in await svc.list(session)}
    assert got["1130"]["is_current"] is True and got["1130"]["cf_activity"] == "OPERATING"
    # 잘못된 활동 값 거부
    with pytest.raises(ValidationError):
        await svc.create(session, code="9999", name="x", kind="ASSET", cf_activity="WRONG")
    # 수정 + 해제
    await svc.update(session, account_id=a.id, is_current=False, cf_activity="INVESTING")
    got2 = {i["code"]: i for i in await svc.list(session)}
    assert got2["1130"]["is_current"] is False and got2["1130"]["cf_activity"] == "INVESTING"
    await svc.update(session, account_id=a.id, clear_is_current=True, clear_cf_activity=True)
    got3 = {i["code"]: i for i in await svc.list(session)}
    assert got3["1130"]["is_current"] is None and got3["1130"]["cf_activity"] is None


async def test_ifrs_element_tag_and_taxonomy_export(session):
    """G10 기초 — ifrs_element 태그 설정/해제·개념 태깅 export·커버리지."""
    a = await svc.create(session, code="1110", name="현금", kind="ASSET", is_current=True,
                         cf_activity="OPERATING", ifrs_element="ifrs-full:CashAndCashEquivalents")
    await svc.create(session, code="4110", name="수익", kind="REVENUE")   # element 미지정
    got = {i["code"]: i for i in await svc.list(session)}
    assert got["1110"]["ifrs_element"] == "ifrs-full:CashAndCashEquivalents"
    tax = await svc.statements_taxonomy(session)
    codes = {i["code"] for i in tax["items"]}
    assert "1110" in codes and "4110" not in codes           # 태그된 계정만 export
    assert tax["tagged_count"] == 1 and tax["total"] == 2
    cov = await svc.classification_coverage(session)
    assert "4110" in cov["missing_ifrs_element"]["codes"]     # 미태그 감사
    assert "1110" not in cov["missing_ifrs_element"]["codes"]
    # 해제
    await svc.update(session, account_id=a.id, clear_ifrs_element=True)
    assert {i["code"]: i for i in await svc.list(session)}["1110"]["ifrs_element"] is None


async def test_classification_coverage_lists_unmapped(session):
    """G8 커버리지 — is_current/cf_activity 미지정 계정 감사(폴백 의존 가시화)."""
    await svc.create(session, code="1130", name="미수금", kind="ASSET",
                     is_current=True, cf_activity="OPERATING")
    await svc.create(session, code="1710", name="유형자산", kind="ASSET")   # 미지정
    await svc.create(session, code="4110", name="수익", kind="REVENUE")     # BS 계정 아님 → 제외
    cov = await svc.classification_coverage(session)
    assert "1710" in cov["missing_is_current"]["codes"]
    assert "1130" not in cov["missing_is_current"]["codes"]
    assert "1710" in cov["missing_cf_activity"]["codes"]
    assert "4110" not in cov["missing_cf_activity"]["codes"]   # REVENUE는 cf 대상 아님
    assert cov["missing_is_current"]["count"] >= 1
