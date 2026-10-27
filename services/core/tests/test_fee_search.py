"""수가/약제 마스터 검색 (약제급여목록 적재 후)."""
from datetime import date

from sqlalchemy import select  # noqa: F401

from erp.regulatory.models import FeeMaster
from erp.regulatory.router import search_fees
from erp.common.router import CurrentUser

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def _seed(session):
    session.add_all([
        FeeMaster(code="645302132", code_type="DRUG", name="포크랄시럽(포수클로랄)",
                  unit_price=129, price_cap=129, criteria_text="chloral hydrate | 한림제약",
                  valid_from=date(2026, 6, 1)),
        FeeMaster(code="651904420", code_type="DRUG", name="명세핀정3밀리그램",
                  unit_price=96, price_cap=96, criteria_text="doxepin | 명인제약",
                  valid_from=date(2026, 6, 1)),
        FeeMaster(code="AA100", code_type="TARIFF", name="외래 진찰료", unit_price=17610,
                  valid_from=date(2026, 1, 1)),
    ])
    await session.flush()


async def test_search_by_name(session):
    await _seed(session)
    out = await search_fees(q="포크랄", user=ADMIN, session=session)
    assert out["count"] == 1 and out["items"][0]["code"] == "645302132"


async def test_search_by_ingredient(session):
    await _seed(session)
    out = await search_fees(q="doxepin", code_type="DRUG", user=ADMIN, session=session)
    assert out["count"] == 1 and out["items"][0]["name"].startswith("명세핀")


async def test_search_type_filter(session):
    await _seed(session)
    out = await search_fees(q="진찰", code_type="DRUG", user=ADMIN, session=session)
    assert out["count"] == 0                       # 진찰료는 TARIFF
    out2 = await search_fees(q="진찰", code_type="TARIFF", user=ADMIN, session=session)
    assert out2["count"] == 1
