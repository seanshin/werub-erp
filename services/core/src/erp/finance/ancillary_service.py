"""부대사업 수익(의료외수익·내부운영 갭 감사 Tier1·마이그 0215) — 비진료 수익 관리.

주차·임대·비보험검진·식당 등 의료외수익(병원회계기준 별도 분류 4130). 범용 설정형 — 기관이 실제
운영하는 항목만 마스터에 활성화. 부대사업은 부가세 원칙 과세라 taxable 항목은 수납액을 순액(4130)+
매출세액(2135)으로 분리(면세인 의료용역과 구분). 회계: DR 현금(1110)/카드미수(1132) / CR 4130 (+2135).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import AncillaryRevenue, AncillaryRevenueItem
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_CASH = "1110"
ACC_CARD_AR = "1132"
ACC_VAT_PAYABLE = "2135"   # 부가세예수금(매출세액)
_CATEGORIES = {"PARKING", "RENTAL", "CAFETERIA", "CHECKUP", "FUNERAL", "OTHER"}
VAT_RATE = 0.10


class AncillaryRevenueService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()

    # ── 항목 마스터 ─────────────────────────────────────────
    async def create_item(self, session: AsyncSession, *, name: str, category: str,
                          taxable: bool = True, default_price: float | None = None,
                          account_code: str = "4130",
                          note: str | None = None) -> AncillaryRevenueItem:
        cat = (category or "OTHER").upper()
        if cat not in _CATEGORIES:
            raise ValidationError("분류 오류", {"category": category})
        if not (name or "").strip():
            raise ValidationError("항목명은 필수입니다")
        item = AncillaryRevenueItem(name=name[:80], category=cat, taxable=taxable,
                                    default_price=round(default_price) if default_price else None,
                                    account_code=account_code, note=note)
        session.add(item)
        await session.flush()
        return item

    async def set_item_active(self, session: AsyncSession, *, item_id: int,
                              active: bool) -> AncillaryRevenueItem:
        item = await session.get(AncillaryRevenueItem, item_id)
        if item is None:
            raise ValidationError("존재하지 않는 항목입니다", {"item_id": item_id})
        item.active = active
        return item

    async def list_items(self, session: AsyncSession, *, active_only: bool = False) -> list[dict]:
        q = select(AncillaryRevenueItem)
        if active_only:
            q = q.where(AncillaryRevenueItem.active.is_(True))
        rows = (await session.scalars(q.order_by(AncillaryRevenueItem.category,
                                                 AncillaryRevenueItem.name))).all()
        return [{"id": i.id, "name": i.name, "category": i.category, "taxable": i.taxable,
                 "account_code": i.account_code,
                 "default_price": float(i.default_price) if i.default_price is not None else None,
                 "active": i.active, "note": i.note} for i in rows]

    # ── 수납(발생) ──────────────────────────────────────────
    async def record(self, session: AsyncSession, *, item_id: int, amount: float,
                     method: str = "CASH", occurred_on: date | None = None, qty: float = 1,
                     note: str | None = None, actor: int | None = None,
                     client_ref: str | None = None) -> AncillaryRevenue:
        """의료외수익 수납 — 과세 항목은 순액/매출세액 분리 전기.

        멱등(F5): client_ref 제공 시 동일 키 기존 수납이 있으면 재전송(네트워크 재시도)으로 보고
        기존 레코드 반환(중복 수익/전표 미생성). 부분 유니크 인덱스가 동시 중복의 백스톱."""
        if client_ref:
            dup = await session.scalar(select(AncillaryRevenue).where(
                AncillaryRevenue.client_ref == client_ref))
            if dup is not None:
                return dup
        item = await session.get(AncillaryRevenueItem, item_id)
        if item is None:
            raise ValidationError("존재하지 않는 항목입니다", {"item_id": item_id})
        if not item.active:
            raise ValidationError("비활성 항목입니다", {"item_id": item_id})
        gross = round(float(amount))
        if gross <= 0:
            raise ValidationError("수납액은 0보다 커야 합니다")
        m = (method or "CASH").upper()
        if item.taxable:
            net = round(gross / (1 + VAT_RATE))     # 순매출 = 총액 / 1.1
            vat = gross - net
        else:
            net, vat = gross, 0
        occurred_on = occurred_on or date.today()
        rev = AncillaryRevenue(item_id=item_id, amount=gross, net_amount=net, vat_amount=vat,
                               method=m, qty=qty, occurred_on=occurred_on, note=note,
                               created_by=actor, client_ref=client_ref)
        session.add(rev)
        await session.flush()
        debit = ACC_CARD_AR if m == "CARD" else ACC_CASH
        lines = [VoucherLineDTO(debit, "DEBIT", gross),
                 VoucherLineDTO(item.account_code, "CREDIT", net)]
        if vat > 0:
            lines.append(VoucherLineDTO(ACC_VAT_PAYABLE, "CREDIT", vat))
        await self._vs.create(session, source_module="ancillary",
                              source_ref=f"ancillary:{rev.id}", voucher_on=occurred_on,
                              lines=lines, created_by=actor,
                              memo=f"의료외수익 {item.category} {item.name}")
        rev.voucher_ref = f"ancillary:{rev.id}"
        return rev

    async def summary(self, session: AsyncSession, *, since: date, until: date) -> dict:
        """기간 의료외수익 — 카테고리별 총액·순매출·매출세액(부가세 신고 보조)."""
        rows = (await session.execute(
            select(AncillaryRevenueItem.category,
                   func.coalesce(func.sum(AncillaryRevenue.amount), 0),
                   func.coalesce(func.sum(AncillaryRevenue.net_amount), 0),
                   func.coalesce(func.sum(AncillaryRevenue.vat_amount), 0),
                   func.count())
            .join(AncillaryRevenueItem, AncillaryRevenueItem.id == AncillaryRevenue.item_id)
            .where(AncillaryRevenue.occurred_on >= since, AncillaryRevenue.occurred_on <= until)
            .group_by(AncillaryRevenueItem.category))).all()
        items = [{"category": c, "gross": float(g), "net": float(n), "vat": float(v),
                  "count": int(cnt)} for c, g, n, v, cnt in rows]
        return {"since": str(since), "until": str(until), "by_category": items,
                "total_gross": sum(i["gross"] for i in items),
                "total_net": sum(i["net"] for i in items),
                "total_vat": sum(i["vat"] for i in items)}
