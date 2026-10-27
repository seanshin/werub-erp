"""리스 회계 (K-IFRS 16) — 사용권자산·리스부채 인식 + 월별 이자·원금상환·상각.

개시: 리스부채 = 월 지급액 현가(연금현가), 사용권자산 = 리스부채. 전표 1720/2210.
월: 이자=리스부채×월율, 원금=지급액−이자, 상각=ROU/기간. 전표 5630·2210·1110 + 5640/1825.
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import LeaseAccrual, LeaseContract
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_ROU = "1720"        # 사용권자산
ACC_ROU_ACCUM = "1825"  # 사용권자산상각누계액
ACC_LEASE_LIAB = "2210"  # 리스부채
ACC_INTEREST = "5630"   # 리스이자비용
ACC_AMORT = "5640"      # 사용권자산상각비
ACC_SHORT_LEASE = "5645"  # 단기·소액리스료(면제, K-IFRS 1116)
ACC_CASH = "1110"


def _present_value(payment: float, n: int, monthly_rate: float) -> float:
    if monthly_rate <= 0:
        return payment * n
    return payment * (1 - (1 + monthly_rate) ** -n) / monthly_rate


class LeaseService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def create(self, session: AsyncSession, *, lease_no: str, asset_name: str,
                     start_on: date, term_months: int, monthly_payment: float,
                     annual_rate: float = 0, lessor: str = "", exempt: bool = False,
                     asset_kind: str = "GENERAL", itam_asset_id: int | None = None,
                     user_id: int | None = None) -> LeaseContract:
        """리스개시 — 리스부채(현가)·사용권자산 인식 전표. exempt=단기·소액 면제(인식 생략).

        asset_kind='IT'+itam_asset_id: IT자산 리스 취득(0227·itam에서 호출)."""
        if term_months <= 0 or monthly_payment <= 0:
            raise ValidationError("리스 기간·지급액은 0보다 커야 합니다")
        # 단기(≤12개월)·소액 리스 면제 — 사용권자산/리스부채 미인식, 월 리스료 직접 비용(개시 전표 없음)
        liability = 0 if exempt else round(_present_value(monthly_payment, term_months, annual_rate / 12))
        lease = LeaseContract(
            lease_no=lease_no, asset_name=asset_name, lessor=lessor or None, start_on=start_on,
            term_months=term_months, monthly_payment=monthly_payment, annual_rate=annual_rate,
            rou_initial=liability, liability_initial=liability, current_liability=liability,
            exempt=exempt, status="ACTIVE", asset_kind=asset_kind, itam_asset_id=itam_asset_id,
            created_by=user_id)   # 면제 구분은 exempt 플래그
        session.add(lease)
        await session.flush()
        if not exempt:
            await self._vs.create(
                session, source_module="lease", source_ref=f"lease-start:{lease.id}",
                voucher_on=start_on, created_by=user_id, memo=f"리스개시: {asset_name}",
                lines=[VoucherLineDTO(ACC_ROU, "DEBIT", liability),
                       VoucherLineDTO(ACC_LEASE_LIAB, "CREDIT", liability)])
        return lease

    async def remeasure(self, session: AsyncSession, *, lease_id: int, new_monthly_payment: float,
                        new_term_months: int, on_date: date, user_id: int | None = None) -> dict:
        """리스변경 재측정(K-IFRS 1116) — 잔여기간 신지급액 현가로 리스부채 재측정, 사용권자산 동액 조정."""
        lease = await session.get(LeaseContract, lease_id)
        if lease is None:
            raise ValidationError("존재하지 않는 리스입니다")
        if lease.exempt:
            raise ValidationError("면제 리스는 재측정 대상이 아닙니다")
        if lease.status == "ENDED":
            raise ValidationError("종료된 리스입니다")
        remaining = new_term_months - lease.periods_accrued
        if remaining <= 0:
            raise ValidationError("잔여기간(신규 총기간−경과)이 0보다 커야 합니다")
        new_liab = round(_present_value(new_monthly_payment, remaining, float(lease.annual_rate) / 12))
        delta = new_liab - round(float(lease.current_liability))
        sref = f"lease-remeasure:{lease_id}:{on_date}"
        if delta:                # VoucherService.create가 source_ref로 멱등 보장
            if delta > 0:        # 부채↑ → 사용권자산↑
                lines = [VoucherLineDTO(ACC_ROU, "DEBIT", delta),
                         VoucherLineDTO(ACC_LEASE_LIAB, "CREDIT", delta)]
            else:                # 부채↓ → 사용권자산↓
                lines = [VoucherLineDTO(ACC_LEASE_LIAB, "DEBIT", -delta),
                         VoucherLineDTO(ACC_ROU, "CREDIT", -delta)]
            await self._vs.create(
                session, source_module="lease", source_ref=sref, voucher_on=on_date,
                created_by=user_id, memo=f"리스 재측정: {lease.asset_name}", lines=lines)
        lease.current_liability = new_liab
        lease.rou_initial = round(float(lease.rou_initial) + delta)   # 잔여 상각 기준 조정
        lease.monthly_payment = new_monthly_payment
        lease.term_months = new_term_months
        return {"lease_id": lease_id, "delta": delta, "liability_after": new_liab,
                "term_months": new_term_months}

    async def maturity(self, session: AsyncSession) -> dict:
        """리스부채 만기분석(K-IFRS 1116 공시) — 잔여 지급액(미할인)을 만기버킷으로, 현재 부채(현가) 합."""
        rows = (await session.execute(select(LeaseContract).where(
            LeaseContract.exempt.is_(False), LeaseContract.status != "ENDED"))).scalars().all()
        buckets = {"≤1년": 0.0, "1-2년": 0.0, "2-5년": 0.0, ">5년": 0.0}
        pv_total = 0.0
        for ls in rows:
            rem = ls.term_months - ls.periods_accrued
            pay = float(ls.monthly_payment)
            pv_total += float(ls.current_liability)
            for k in range(rem):       # 잔여 각 월을 만기버킷에 배분(미할인 지급액)
                months_out = k + 1
                key = ("≤1년" if months_out <= 12 else "1-2년" if months_out <= 24
                       else "2-5년" if months_out <= 60 else ">5년")
                buckets[key] += pay
        return {"buckets": [{"bucket": k, "undiscounted": round(v)} for k, v in buckets.items()],
                "present_value_total": round(pv_total)}

    async def accrue_monthly(self, session: AsyncSession, *, lease_id: int, period: str,
                             user_id: int | None = None) -> dict:
        """월별 리스 회계 — 이자·원금상환·상각 전표. 멱등(계약·기간)."""
        lease = await session.get(LeaseContract, lease_id)
        if lease is None:
            raise ValidationError("존재하지 않는 리스입니다")
        dup = await session.scalar(select(LeaseAccrual).where(
            LeaseAccrual.lease_id == lease_id, LeaseAccrual.period == period))
        if dup is not None:
            return {"lease_id": lease_id, "period": period, "already": True}
        if lease.periods_accrued >= lease.term_months:
            raise ValidationError("리스 기간이 종료됐습니다")
        on = date(int(period[:4]), int(period[5:7]), 1)
        if lease.exempt:        # 단기·소액 면제 — 월 리스료 직접 비용(사용권/이자/상각 없음)
            pay = round(float(lease.monthly_payment))
            ref = f"lease:{lease_id}:{period}"
            await self._vs.create(
                session, source_module="lease", source_ref=ref, voucher_on=on, created_by=user_id,
                memo=f"단기·소액 리스료 {period}: {lease.asset_name}",
                lines=[VoucherLineDTO(ACC_SHORT_LEASE, "DEBIT", pay),
                       VoucherLineDTO(ACC_CASH, "CREDIT", pay)])
            lease.periods_accrued += 1
            if lease.periods_accrued >= lease.term_months:
                lease.status = "ENDED"
            session.add(LeaseAccrual(lease_id=lease_id, period=period, interest=0,
                                     principal=0, amortization=0, voucher_ref=ref))
            return {"lease_id": lease_id, "period": period, "exempt": True, "lease_expense": pay}
        r = float(lease.annual_rate) / 12
        liab = float(lease.current_liability)
        pay = float(lease.monthly_payment)
        interest = round(liab * r)
        is_final = (lease.periods_accrued + 1) >= lease.term_months
        if is_final:    # 최종기 — 잔여 부채·ROU를 0으로 true-up(반올림 drift 제거)
            principal = round(liab)
            amort = round(float(lease.rou_initial) - float(lease.accumulated_amort))
        else:
            principal = round(pay - interest)
            # ROU 상각 = 현 장부가(rou_initial−상각누계)를 **잔여기간**으로 정액 배분(IFRS16).
            # 미상각 시 base케이스는 rou_initial/term_months와 동일하나, 재측정(remeasure) 후엔
            # 조정된 rou_initial을 전체 term으로 나눠 잔여기간 과소상각·최종기 큰 plug가 발생했음.
            remaining = lease.term_months - lease.periods_accrued
            rou_carrying = float(lease.rou_initial) - float(lease.accumulated_amort)
            amort = round(rou_carrying / remaining) if remaining > 0 else round(rou_carrying)
        cash = interest + principal       # 차대 균형 보장(정상기엔 = 월지급액)
        ref = f"lease:{lease_id}:{period}"
        await self._vs.create(
            session, source_module="lease", source_ref=ref, voucher_on=on, created_by=user_id,
            memo=f"리스 회계 {period}: {lease.asset_name}",
            lines=[VoucherLineDTO(ACC_INTEREST, "DEBIT", interest),
                   VoucherLineDTO(ACC_LEASE_LIAB, "DEBIT", principal),
                   VoucherLineDTO(ACC_CASH, "CREDIT", cash),
                   VoucherLineDTO(ACC_AMORT, "DEBIT", amort),
                   VoucherLineDTO(ACC_ROU_ACCUM, "CREDIT", amort)])
        lease.current_liability = liab - principal
        lease.accumulated_amort = float(lease.accumulated_amort) + amort
        lease.periods_accrued += 1
        if lease.periods_accrued >= lease.term_months:
            lease.status = "ENDED"
        session.add(LeaseAccrual(lease_id=lease_id, period=period, interest=interest,
                                 principal=principal, amortization=amort, voucher_ref=ref))
        return {"lease_id": lease_id, "period": period, "interest": interest,
                "principal": principal, "amortization": amort,
                "liability_after": round(lease.current_liability)}

    async def list_leases(self, session: AsyncSession) -> list[dict]:
        rows = (await session.execute(
            select(LeaseContract).order_by(LeaseContract.id.desc()))).scalars().all()
        return [{"id": x.id, "lease_no": x.lease_no, "asset_name": x.asset_name,
                 "lessor": x.lessor, "start_on": str(x.start_on), "term_months": x.term_months,
                 "monthly_payment": float(x.monthly_payment),
                 "rou_initial": float(x.rou_initial),
                 "rou_book": float(x.rou_initial) - float(x.accumulated_amort),
                 "current_liability": float(x.current_liability),
                 "periods_accrued": x.periods_accrued, "status": x.status,
                 "asset_kind": x.asset_kind, "itam_asset_id": x.itam_asset_id} for x in rows]
