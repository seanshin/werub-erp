"""무형자산(K-IFRS 1038)·차입원가 자본화(1023)·손상(1036).

취득(1850/현금·미지급)·정액 상각(5215/1855, 월·멱등)·차입원가 자본화(적격자산 원가 가산)·
손상차손(5216/1856)·환입(1856/4641, 누계 한도)·처분(1850 제거·누계 상계·처분손익 4615/5616).
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import IntangibleAsset, Voucher
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_INTANGIBLE = "1850"      # 무형자산
ACC_INTANGIBLE_ACCUM = "1855"  # 무형자산상각누계
ACC_AMORT_EXP = "5215"       # 무형자산상각비
ACC_PAYABLE = "2110"         # 미지급금
ACC_CASH = "1110"
ACC_IMPAIR_LOSS = "5216"     # 무형자산손상차손(비용, 1036)
ACC_IMPAIR_ACCUM = "1856"    # 무형자산손상차손누계(자산 차감)
ACC_IMPAIR_REVERSAL = "4641"  # 무형자산손상차손환입(수익, 1036)
ACC_DISPOSAL_GAIN = "4615"   # 무형자산처분이익
ACC_DISPOSAL_LOSS = "5616"   # 무형자산처분손실


class IntangibleAssetService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def create(self, session: AsyncSession, *, asset_no: str, name: str, acquired_on: date,
                     cost: float, useful_life_years: int, on_credit: bool = False,
                     user_id: int | None = None) -> dict:
        """무형자산 취득 — 1850/현금(또는 미지급)."""
        if cost <= 0 or useful_life_years <= 0:
            raise ValidationError("취득원가·내용연수는 0보다 커야 합니다")
        ia = IntangibleAsset(asset_no=asset_no, name=name, acquired_on=acquired_on,
                             cost=round(cost), useful_life_years=useful_life_years)
        session.add(ia)
        await session.flush()
        credit = ACC_PAYABLE if on_credit else ACC_CASH
        await self._vs.create(
            session, source_module="intangible", source_ref=f"intangible-acq:{ia.id}",
            voucher_on=acquired_on, created_by=user_id, memo=f"무형자산 취득: {name}",
            lines=[VoucherLineDTO(ACC_INTANGIBLE, "DEBIT", round(cost)),
                   VoucherLineDTO(credit, "CREDIT", round(cost))])
        return {"id": ia.id, "asset_no": asset_no, "cost": round(cost)}

    async def amortize_monthly(self, session: AsyncSession, *, asset_id: int, period: str,
                               user_id: int | None = None) -> dict:
        """월 정액 상각 — (원가+자본화차입원가)/내용연수/12. 멱등(자산·기간)."""
        if (len(period) != 7 or period[4] != "-" or not period[:4].isdigit()
                or not period[5:7].isdigit() or not 1 <= int(period[5:7]) <= 12):
            raise ValidationError("기간 형식은 YYYY-MM 이어야 합니다")
        ia = await session.get(IntangibleAsset, asset_id)
        if ia is None:
            raise ValidationError("존재하지 않는 무형자산입니다")
        if ia.status == "DISPOSED":
            raise ValidationError("처분된 무형자산입니다")
        sref = f"intangible-amort:{asset_id}:{period}"
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "intangible", Voucher.source_ref == sref)):
            return {"id": asset_id, "period": period, "already": True}
        base = float(ia.cost) + float(ia.capitalized_borrowing)
        amort = round(base / ia.useful_life_years / 12)
        # 손상누계도 차감 — 손상 인식 자산이 음수 장부가까지 과다상각되지 않도록(IAS 36/38:
        # 손상 후 상각은 회복가능 장부가 한도). 잔여=원가-상각누계-손상누계.
        remaining = round(base - float(ia.accumulated_amort) - float(ia.accumulated_impairment))
        if remaining <= 0:
            raise ValidationError("상각 완료된 무형자산입니다")
        amort = min(amort, remaining)
        on = date(int(period[:4]), int(period[5:7]), monthrange(int(period[:4]), int(period[5:7]))[1])
        await self._vs.create(
            session, source_module="intangible", source_ref=sref, voucher_on=on, created_by=user_id,
            memo=f"무형자산 상각 {period}: {ia.name}",
            lines=[VoucherLineDTO(ACC_AMORT_EXP, "DEBIT", amort),
                   VoucherLineDTO(ACC_INTANGIBLE_ACCUM, "CREDIT", amort)])
        ia.accumulated_amort = float(ia.accumulated_amort) + amort
        return {"id": asset_id, "period": period, "amortization": amort,
                "book_value": round(self._book_value(ia))}

    async def capitalize_borrowing(self, session: AsyncSession, *, asset_id: int, amount: float,
                                   on_date: date, user_id: int | None = None) -> dict:
        """차입원가 자본화(K-IFRS 1023) — 적격자산 건설기간 차입원가를 원가 가산(1850/미지급금)."""
        ia = await session.get(IntangibleAsset, asset_id)
        if ia is None:
            raise ValidationError("존재하지 않는 무형자산입니다")
        if amount <= 0:
            raise ValidationError("자본화액은 0보다 커야 합니다")
        amount = round(amount)
        # 멱등 가드 — amortize/impair와 동일. VoucherService.create는 (module,ref) 충돌 시
        # 기존 전표를 반환하나, 가드가 없으면 보조부(capitalized_borrowing)만 이중 가산된다.
        sref = f"intangible-borrow:{asset_id}:{on_date}"
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "intangible", Voucher.source_ref == sref)):
            return {"id": asset_id, "capitalized": 0, "already": True,
                    "total_capitalized": round(float(ia.capitalized_borrowing))}
        await self._vs.create(
            session, source_module="intangible", source_ref=sref,
            voucher_on=on_date, created_by=user_id, memo=f"차입원가 자본화: {ia.name}",
            lines=[VoucherLineDTO(ACC_INTANGIBLE, "DEBIT", amount),
                   VoucherLineDTO(ACC_PAYABLE, "CREDIT", amount)])
        ia.capitalized_borrowing = float(ia.capitalized_borrowing) + amount
        return {"id": asset_id, "capitalized": amount,
                "total_capitalized": round(float(ia.capitalized_borrowing))}

    @staticmethod
    def _book_value(ia: IntangibleAsset) -> float:
        return (float(ia.cost) + float(ia.capitalized_borrowing)
                - float(ia.accumulated_amort) - float(ia.accumulated_impairment))

    async def impair(self, session: AsyncSession, *, asset_id: int, recoverable_amount: float,
                     on_date: date, user_id: int | None = None) -> dict:
        """손상차손(K-IFRS 1036) — 회수가능액 < 장부가 시 차액을 5216/1856 전기. 멱등(자산·일자)."""
        ia = await session.get(IntangibleAsset, asset_id)
        if ia is None:
            raise ValidationError("존재하지 않는 무형자산입니다")
        if ia.status == "DISPOSED":
            raise ValidationError("처분된 무형자산입니다")
        sref = f"intangible-impair:{asset_id}:{on_date}"
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "intangible", Voucher.source_ref == sref)):
            return {"id": asset_id, "skipped": "이미 손상 인식(동일 자산·일자)"}
        book = self._book_value(ia)
        loss = round(book - float(recoverable_amount))
        if loss <= 0:
            raise ValidationError(f"회수가능액이 장부가액({round(book):,}) 이상 — 손상 없음")
        await self._vs.create(
            session, source_module="intangible", source_ref=sref, voucher_on=on_date,
            created_by=user_id, memo=f"무형자산 손상차손: {ia.name}",
            lines=[VoucherLineDTO(ACC_IMPAIR_LOSS, "DEBIT", loss),
                   VoucherLineDTO(ACC_IMPAIR_ACCUM, "CREDIT", loss)])
        ia.accumulated_impairment = float(ia.accumulated_impairment) + loss
        return {"id": asset_id, "impairment_loss": loss, "book_value": round(self._book_value(ia))}

    async def reverse_impairment(self, session: AsyncSession, *, asset_id: int,
                                 recoverable_amount: float, on_date: date,
                                 user_id: int | None = None) -> dict:
        """손상환입(K-IFRS 1036) — 회수가능액 회복분을 손상누계 한도 내 1856/4641 전기. 멱등."""
        ia = await session.get(IntangibleAsset, asset_id)
        if ia is None:
            raise ValidationError("존재하지 않는 무형자산입니다")
        if ia.status == "DISPOSED":
            raise ValidationError("처분된 무형자산입니다")
        accum_impair = float(ia.accumulated_impairment)
        if accum_impair <= 0:
            raise ValidationError("환입할 손상누계가 없습니다")
        sref = f"intangible-impair-rev:{asset_id}:{on_date}"
        if await session.scalar(select(Voucher.id).where(
                Voucher.source_module == "intangible", Voucher.source_ref == sref)):
            return {"id": asset_id, "skipped": "이미 환입(동일 자산·일자)"}
        book = self._book_value(ia)
        # 환입 한도 = 손상누계(이전 손상분만 환입 → carrying이 상각후원가 초과 자동 방지)
        reversal = round(min(float(recoverable_amount) - book, accum_impair))
        if reversal <= 0:
            raise ValidationError(f"회수가능액이 장부가({round(book):,}) 이하 — 환입 없음")
        await self._vs.create(
            session, source_module="intangible", source_ref=sref, voucher_on=on_date,
            created_by=user_id, memo=f"무형자산 손상차손환입: {ia.name}",
            lines=[VoucherLineDTO(ACC_IMPAIR_ACCUM, "DEBIT", reversal),
                   VoucherLineDTO(ACC_IMPAIR_REVERSAL, "CREDIT", reversal)])
        ia.accumulated_impairment = accum_impair - reversal
        return {"id": asset_id, "reversal": reversal, "book_value": round(self._book_value(ia))}

    async def dispose(self, session: AsyncSession, *, asset_id: int, disposed_on: date,
                      disposal_amount: float = 0, user_id: int | None = None) -> dict:
        """처분(K-IFRS 1038) — 1850 제거·상각/손상누계 상계·현금·처분손익(4615/5616)."""
        ia = await session.get(IntangibleAsset, asset_id)
        if ia is None:
            raise ValidationError("존재하지 않는 무형자산입니다")
        if ia.status == "DISPOSED":
            raise ValidationError("이미 처분된 무형자산입니다")
        if disposal_amount < 0:
            raise ValidationError("처분금액은 0 이상이어야 합니다")
        gross = round(float(ia.cost) + float(ia.capitalized_borrowing))
        accum_amort = round(float(ia.accumulated_amort))
        accum_impair = round(float(ia.accumulated_impairment))
        book = gross - accum_amort - accum_impair
        proceeds = round(float(disposal_amount))
        gain = proceeds - book
        lines = [VoucherLineDTO(ACC_INTANGIBLE, "CREDIT", gross)]
        if accum_amort > 0:
            lines.append(VoucherLineDTO(ACC_INTANGIBLE_ACCUM, "DEBIT", accum_amort))
        if accum_impair > 0:
            lines.append(VoucherLineDTO(ACC_IMPAIR_ACCUM, "DEBIT", accum_impair))
        if proceeds > 0:
            lines.append(VoucherLineDTO(ACC_CASH, "DEBIT", proceeds))
        if gain > 0:
            lines.append(VoucherLineDTO(ACC_DISPOSAL_GAIN, "CREDIT", gain))
        elif gain < 0:
            lines.append(VoucherLineDTO(ACC_DISPOSAL_LOSS, "DEBIT", -gain))
        await self._vs.create(
            session, source_module="intangible", source_ref=f"intangible-disposal:{asset_id}",
            voucher_on=disposed_on, created_by=user_id, memo=f"무형자산 처분: {ia.name}", lines=lines)
        ia.status, ia.disposed_on = "DISPOSED", disposed_on
        return {"id": asset_id, "book_value": book, "proceeds": proceeds,
                "gain": gain if gain > 0 else 0, "loss": -gain if gain < 0 else 0}

    async def list_assets(self, session: AsyncSession) -> list[dict]:
        rows = (await session.execute(
            select(IntangibleAsset).order_by(IntangibleAsset.id.desc()))).scalars().all()
        return [{"id": x.id, "asset_no": x.asset_no, "name": x.name, "cost": float(x.cost),
                 "capitalized_borrowing": float(x.capitalized_borrowing),
                 "accumulated_amort": float(x.accumulated_amort),
                 "accumulated_impairment": float(x.accumulated_impairment),
                 "book_value": round(self._book_value(x)),
                 "disposed_on": x.disposed_on.isoformat() if x.disposed_on else None,
                 "useful_life_years": x.useful_life_years, "status": x.status} for x in rows]
