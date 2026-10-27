"""차입금 관리 (자금관리 심화) — 차입 실행·월 이자 인식·원금 상환. 리스 패턴 준용.

차입: DR 현금(1110)/CR 차입금(2310 단기·2510 장기). 월 이자: DR 이자비용(5720)/CR 현금(1110),
잔여원금×연이율/12·멱등(loan-int-{id}-{yyyymm}). 상환: DR 차입금/CR 현금. 전액상환 시 REPAID.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import Loan
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_CASH = "1110"
ACC_INTEREST = "5720"
_ACC_BY_KIND = {"SHORT": "2310", "LONG": "2510"}


def _month_end(yyyymm: str) -> date:
    from datetime import timedelta
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)


class LoanService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()

    async def register(self, session: AsyncSession, *, lender: str, principal: float,
                       annual_rate: float, started_on: date, term_months: int,
                       kind: str = "SHORT", actor: int | None = None) -> Loan:
        if principal <= 0 or term_months <= 0:
            raise ValidationError("원금·기간은 0보다 커야 합니다")
        if kind not in _ACC_BY_KIND:
            raise ValidationError("kind는 SHORT|LONG 이어야 합니다")
        loan = Loan(lender=lender, kind=kind, principal=round(principal),
                    annual_rate=annual_rate, started_on=started_on, term_months=term_months,
                    created_by=actor)
        session.add(loan)
        await session.flush()
        await self._vs.create(
            session, source_module="finance", source_ref=f"loan-draw-{loan.id}",
            voucher_on=started_on, created_by=actor, memo=f"차입 실행: {lender}",
            lines=[VoucherLineDTO(ACC_CASH, "DEBIT", round(principal)),
                   VoucherLineDTO(_ACC_BY_KIND[kind], "CREDIT", round(principal))])
        return loan

    async def accrue_interest(self, session: AsyncSession, *, loan_id: int, yyyymm: str,
                              actor: int | None = None) -> dict:
        """월 이자 인식 — 잔여원금×연이율/12. 멱등(월별 1회)."""
        loan = await session.get(Loan, loan_id)
        if loan is None:
            raise ValidationError("존재하지 않는 차입금입니다")
        outstanding = float(loan.principal) - float(loan.repaid_principal)
        interest = round(outstanding * float(loan.annual_rate) / 12)
        if interest <= 0:
            return {"posted": False, "reason": "이자 0", "loan_id": loan_id}
        v = await self._vs.create(
            session, source_module="finance", source_ref=f"loan-int-{loan_id}-{yyyymm}",
            voucher_on=_month_end(yyyymm), created_by=actor, memo=f"차입 이자 {yyyymm}: {loan.lender}",
            lines=[VoucherLineDTO(ACC_INTEREST, "DEBIT", interest),
                   VoucherLineDTO(ACC_CASH, "CREDIT", interest)])
        return {"posted": True, "loan_id": loan_id, "interest": interest,
                "outstanding": round(outstanding), "voucher_id": v.id}

    async def repay(self, session: AsyncSession, *, loan_id: int, amount: float, on: date,
                    actor: int | None = None) -> Loan:
        # 행잠금 — 동시 상환 경합(보조부 이중가산·source_ref 충돌) 방지
        loan = await session.scalar(select(Loan).where(Loan.id == loan_id).with_for_update())
        if loan is None:
            raise ValidationError("존재하지 않는 차입금입니다")
        prior = round(float(loan.repaid_principal))
        outstanding = float(loan.principal) - prior
        if amount <= 0 or round(amount) > round(outstanding):
            raise ValidationError("상환액이 잔여원금을 초과합니다",
                                  {"outstanding": round(outstanding)})
        # source_ref에 직전 누적상환액(prior) 포함 — 매 상환마다 유일(같은 날 동일액 2회도 각각 전기).
        # 과거처럼 (일자·금액)만이면 멱등 충돌로 GL 1건·보조부 2배 가산되던 결함 제거.
        await self._vs.create(
            session, source_module="finance",
            source_ref=f"loan-repay-{loan_id}-{prior}-{round(amount)}",
            voucher_on=on, created_by=actor, memo=f"차입 상환: {loan.lender}",
            lines=[VoucherLineDTO(_ACC_BY_KIND[loan.kind], "DEBIT", round(amount)),
                   VoucherLineDTO(ACC_CASH, "CREDIT", round(amount))])
        loan.repaid_principal = round(prior + amount)
        if round(float(loan.repaid_principal)) >= round(float(loan.principal)):
            loan.status = "REPAID"
        return loan
