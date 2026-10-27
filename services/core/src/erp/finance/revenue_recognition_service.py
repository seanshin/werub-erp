"""수익인식 발생주의 (K-IFRS 1115) — 기간말 미청구수익(계약자산) 발생·차기 환입.

수행의무 이행분 중 미청구분을 기간말에 계약자산(1135)/수익(4110)으로 인식하고, 차기 기초에 환입한다
(실제 청구 시 정상 수익 인식과 상계). 진행률 기반 자동 산정은 HIS 진행 데이터 연동(후속) — 현재는 추정액 입력.
"""
from calendar import monthrange
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import Voucher
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_CONTRACT_ASSET = "1135"   # 계약자산(미청구수익)
ACC_REVENUE = "4110"          # 의료수익


class RevenueRecognitionService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def accrue_unbilled(self, session: AsyncSession, *, yyyymm: str, amount: float,
                              actor: int | None = None) -> dict:
        """기간말 미청구수익 발생(1135/4110) + 차기초 환입(4110/1135). 멱등(기간)."""
        if amount <= 0:
            raise ValueError("발생액은 0보다 커야 합니다")
        amount = round(amount)
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        month_end = date(y, m, monthrange(y, m)[1])
        next_day = month_end + timedelta(days=1)
        # 발생(기간말)
        v1 = await self._vs.create(
            session, source_module="closing-ifrs", source_ref=f"unbilled:{yyyymm}",
            voucher_on=month_end, created_by=actor, memo="미청구수익 발생(K-IFRS 1115)",
            lines=[VoucherLineDTO(ACC_CONTRACT_ASSET, "DEBIT", amount),
                   VoucherLineDTO(ACC_REVENUE, "CREDIT", amount)])
        # 환입(차기 기초) — 실제 청구 시 정상 수익과 상계
        v2 = await self._vs.create(
            session, source_module="closing-ifrs", source_ref=f"unbilled-rev:{yyyymm}",
            voucher_on=next_day, created_by=actor, memo="미청구수익 환입(차기, K-IFRS 1115)",
            lines=[VoucherLineDTO(ACC_REVENUE, "DEBIT", amount),
                   VoucherLineDTO(ACC_CONTRACT_ASSET, "CREDIT", amount)])
        return {"yyyymm": yyyymm, "amount": amount, "accrual_voucher": v1.id,
                "reversal_voucher": v2.id}

    async def status(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """해당 기간 미청구수익 발생 여부."""
        v = await session.scalar(select(Voucher).where(
            Voucher.source_module == "closing-ifrs",
            Voucher.source_ref == f"unbilled:{yyyymm}"))
        return {"yyyymm": yyyymm, "accrued": v is not None,
                "voucher_id": v.id if v else None}
