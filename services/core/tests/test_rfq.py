"""소싱 — RFQ→견적→비교→낙찰→단가계약 + PO 계약단가 자동 적용."""
from datetime import date, timedelta

import pytest

from erp.errors import ValidationError
from erp.scm.models import Item, PriceAgreementLine, Supplier
from erp.scm.po_service import PurchaseOrderService
from erp.scm.rfq_service import PriceAgreementService, RfqService
from sqlalchemy import select

# PO는 오늘 날짜로 발주된다 — 단가계약 유효기간을 고정 연도로 두면 해가 바뀌는 순간
# 계약이 만료돼 '계약단가 적용' 전제가 깨진다(2027-01-01에 깨질 예정이었음).
TODAY = date.today()
rfq_svc = RfqService()
pa_svc = PriceAgreementService()
po_svc = PurchaseOrderService()


async def _seed(session):
    s1 = Supplier(name="A상사", business_no="1110000001")
    s2 = Supplier(name="B상사", business_no="1110000002")
    item = Item(code="X1", name="수액", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add_all([s1, s2, item])
    await session.flush()
    return s1, s2, item


async def _open_rfq(session, item):
    rfq = await rfq_svc.create_rfq(session, actor=1, title="수액 연간 단가",
                                   lines=[{"item_id": item.id, "qty": 1000}])
    await rfq_svc.open_rfq(session, actor=1, rfq_id=rfq.id)
    return rfq


async def test_full_sourcing_flow_award_creates_agreement(session):
    s1, s2, item = await _seed(session)
    rfq = await _open_rfq(session, item)
    q1 = await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                    lines=[{"item_id": item.id, "unit_price": 5000}])
    q2 = await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s2.id,
                                    lines=[{"item_id": item.id, "unit_price": 4500}])
    assert float(q1.total) == 5_000_000 and float(q2.total) == 4_500_000
    ranked = await rfq_svc.compare(session, rfq_id=rfq.id)
    assert ranked[0]["quote_id"] == q2.id and ranked[0]["rank"] == 1   # 최저가 우선
    pa = await rfq_svc.award(session, actor=1, rfq_id=rfq.id, quote_id=q2.id,
                             valid_from=date(2026, 1, 1), valid_to=date(2026, 12, 31))
    assert pa.supplier_id == s2.id
    # 단가계약 라인 생성 + 나머지 견적 반려
    line = (await session.execute(select(PriceAgreementLine).where(
        PriceAgreementLine.agreement_id == pa.id))).scalar_one()
    assert float(line.unit_price) == 4500
    price = await pa_svc.active_price(session, item_id=item.id, supplier_id=s2.id,
                                      on=date(2026, 6, 1))
    assert price == 4500


async def test_po_applies_agreement_price(session):
    s1, s2, item = await _seed(session)
    rfq = await _open_rfq(session, item)
    q = await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": item.id, "unit_price": 4800}])
    pa = await rfq_svc.award(session, actor=1, rfq_id=rfq.id, quote_id=q.id,
                             valid_from=TODAY - timedelta(days=30),
                             valid_to=TODAY + timedelta(days=30))
    # PO가 단가계약 참조 → 계약단가 자동 적용(라인 단가 무시)
    po = await po_svc.request(session, supplier_id=s1.id, actor=1,
                              lines=[{"item_id": item.id, "qty": 10, "unit_price": 9999}],
                              price_agreement_id=pa.id)
    total = await po_svc._po_total(session, po.id)
    assert total == 48000      # 4800 × 10 (계약단가 적용, 9999 무시)


async def test_po_rejects_item_not_in_agreement(session):
    s1, s2, item = await _seed(session)
    other = Item(code="X2", name="거즈", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add(other)
    await session.flush()
    rfq = await _open_rfq(session, item)
    q = await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": item.id, "unit_price": 4800}])
    pa = await rfq_svc.award(session, actor=1, rfq_id=rfq.id, quote_id=q.id,
                             valid_from=TODAY - timedelta(days=30),
                             valid_to=TODAY + timedelta(days=30))
    with pytest.raises(ValidationError, match="단가계약에 없는 품목"):
        await po_svc.request(session, supplier_id=s1.id, actor=1,
                             lines=[{"item_id": other.id, "qty": 5, "unit_price": 100}],
                             price_agreement_id=pa.id)


async def test_quote_must_cover_all_rfq_items(session):
    s1, s2, item = await _seed(session)
    item2 = Item(code="X3", name="주사기", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add(item2)
    await session.flush()
    rfq = await rfq_svc.create_rfq(session, actor=1, title="t",
                                   lines=[{"item_id": item.id, "qty": 10},
                                          {"item_id": item2.id, "qty": 5}])
    await rfq_svc.open_rfq(session, actor=1, rfq_id=rfq.id)
    with pytest.raises(ValidationError, match="전 품목"):     # item2 누락
        await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": item.id, "unit_price": 100}])


async def test_quote_rejects_extra_and_duplicate(session):
    s1, s2, item = await _seed(session)
    other = Item(code="X4", name="외품목", category="SUPPLY", is_narcotic=False, safety_stock_qty=1)
    session.add(other)
    await session.flush()
    rfq = await _open_rfq(session, item)
    with pytest.raises(ValidationError, match="RFQ에 없는"):
        await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": other.id, "unit_price": 100}])
    with pytest.raises(ValidationError, match="중복"):
        await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": item.id, "unit_price": 100},
                                          {"item_id": item.id, "unit_price": 200}])


async def test_quote_only_when_open(session):
    s1, s2, item = await _seed(session)
    rfq = await rfq_svc.create_rfq(session, actor=1, title="t",
                                   lines=[{"item_id": item.id, "qty": 10}])  # DRAFT
    with pytest.raises(ValidationError, match="견적 접수"):
        await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                                   lines=[{"item_id": item.id, "unit_price": 100}])


async def test_list_rfqs_and_detail(session):
    s1, s2, item = await _seed(session)
    rfq = await _open_rfq(session, item)
    await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s1.id,
                               lines=[{"item_id": item.id, "unit_price": 5000}])
    await rfq_svc.submit_quote(session, actor=1, rfq_id=rfq.id, supplier_id=s2.id,
                               lines=[{"item_id": item.id, "unit_price": 4500}])
    rows = await rfq_svc.list_rfqs(session)
    row = next(x for x in rows if x["id"] == rfq.id)
    assert row["line_count"] == 1 and row["quote_count"] == 2 and row["status"] == "OPEN"
    d = await rfq_svc.detail(session, rfq_id=rfq.id)
    assert d["title"] == "수액 연간 단가" and len(d["lines"]) == 1
    # 견적은 총액 오름차순(최저가 먼저)
    assert [q["total"] for q in d["quotes"]] == [4_500_000.0, 5_000_000.0]
    # status 필터
    assert all(x["status"] == "OPEN" for x in await rfq_svc.list_rfqs(session, status="OPEN"))
