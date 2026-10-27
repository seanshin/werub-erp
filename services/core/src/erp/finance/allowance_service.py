"""대손충당금 / 기대신용손실 (K-IFRS 1109·1037) — 연령기반 간이 ECL.

미수금(AR) 연령버킷 잔액 × 버킷별 요율 = 목표 충당액. 현 충당잔액(1190 대변)과 차액만 결산분개:
증가는 대손상각비(5690)/대손충당금(1190), 환입은 반대. 멱등(월별 1전표). 모든 조정=전표.
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import AllowanceRate, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.finance.settlement_service import SettlementService

ACC_ALLOWANCE = "1190"   # 대손충당금(자산 차감)
ACC_BADDEBT = "5690"     # 대손상각비
_DEFAULT_RATES = {"current": 0.005, "1-30": 0.02, "31-60": 0.10, "61-90": 0.30, "90+": 0.50}


def _month_end(yyyymm: str) -> date:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, monthrange(y, m)[1])


class AllowanceService:
    def __init__(self) -> None:
        self._vs = VoucherService()
        self._settle = SettlementService()

    async def rates(self, session: AsyncSession, *, kind: str = "AR") -> dict[str, float]:
        # OVERLAY 버킷은 연령요율이 아니라 forward-looking 계수(overlay_factor) → 제외.
        # 포함하면 버킷요율 미시드 시 {OVERLAY:..}가 truthy가 되어 _DEFAULT_RATES 폴백이
        # 무력화되고 전 버킷 요율이 0으로 떨어져 충당금이 전액 환입된다.
        rows = (await session.execute(select(AllowanceRate).where(
            AllowanceRate.kind == kind, AllowanceRate.bucket != "OVERLAY"))).scalars().all()
        # 기본요율 위에 DB 오버레이 병합 — 부분 시드 시 누락 버킷이 요율 0으로 떨어져 대손이
        # 과소계상되던 것 방지(계산검토 M-2). 명시적 0 시드는 여전히 오버라이드 가능.
        return {**_DEFAULT_RATES, **{x.bucket: float(x.rate) for x in rows}}

    async def overlay_factor(self, session: AsyncSession, *, kind: str = "AR") -> float:
        """forward-looking 거시전망 오버레이 계수(IFRS 9) — 기본 1.0(미설정 시)."""
        f = (await session.execute(select(AllowanceRate.rate).where(
            AllowanceRate.kind == kind, AllowanceRate.bucket == "OVERLAY"))).scalar()
        return float(f) if f is not None else 1.0

    async def set_overlay(self, session: AsyncSession, *, factor: float,
                          kind: str = "AR", actor: int | None = None) -> dict:
        """오버레이 계수 설정(경기전망 반영). 0.1~5.0."""
        if not 0.1 <= factor <= 5.0:
            raise ValueError("오버레이 계수는 0.1~5.0 범위")
        row = (await session.execute(select(AllowanceRate).where(
            AllowanceRate.kind == kind, AllowanceRate.bucket == "OVERLAY"))).scalar_one_or_none()
        if row is None:
            row = AllowanceRate(kind=kind, bucket="OVERLAY", rate=factor, updated_by=actor)
            session.add(row)
        else:
            row.rate, row.updated_by = factor, actor
        await session.flush()
        return {"kind": kind, "overlay": factor}

    async def _allowance_balance(self, session: AsyncSession, *, as_of: date,
                                 code: str = ACC_ALLOWANCE) -> float:
        d, cr = (await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == code, Voucher.voucher_on <= as_of,
                   # 미승인·반려 수동전표 배제(statements 규약 — 계산검토 M-1)
                   Voucher.status.in_(("POSTED", "REVERSED"))))).one()
        return float(cr) - float(d)         # 대손충당금=대변잔액

    async def preview(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """충당 미리보기 — 버킷별 잔액·요율·충당액, 목표/현재/조정차액."""
        until = _month_end(yyyymm)
        # reconstruct=True — 대손충당(IFRS 9 ECL)은 보고시점(월말) 채권 잔액 기준. as_of 재구성으로
        # 과거 월 재실행/백필이 안정. 회수 복원은 source_module별 재구성기(1130=ar-collect 전표,
        # 1131 환자수납=patient 등록 재구성기·Payment 기반)로 전 AR 계정에 정확(의존성 역전).
        aging = await self._settle.aging(session, as_of=until, kind="AR", reconstruct=True)
        rates = await self.rates(session, kind="AR")
        overlay = await self.overlay_factor(session, kind="AR")
        lines, base = [], 0.0
        for b, bal in aging["buckets"].items():
            rate = rates.get(b, 0.0)
            amt = bal * rate
            base += amt
            lines.append({"bucket": b, "balance": round(bal), "rate": rate,
                          "allowance": round(amt)})
        required = base * overlay        # forward-looking 거시전망 오버레이(IFRS 9)
        current = await self._allowance_balance(session, as_of=until)
        return {"yyyymm": yyyymm, "as_of": str(until), "buckets": lines,
                "base_ecl": round(base), "overlay": overlay, "required": round(required),
                "current": round(current), "delta": round(required) - round(current)}

    async def post(self, session: AsyncSession, *, yyyymm: str,
                   actor: int | None = None) -> dict:
        """충당 차액 결산분개(멱등). delta>0 전입(5690/1190), delta<0 환입(1190/5690)."""
        p = await self.preview(session, yyyymm=yyyymm)
        delta = p["delta"]
        if delta == 0:
            return {"posted": False, "reason": "조정 없음", **p}
        until = _month_end(yyyymm)
        if delta > 0:
            lines = [VoucherLineDTO(ACC_BADDEBT, "DEBIT", delta),
                     VoucherLineDTO(ACC_ALLOWANCE, "CREDIT", delta)]
        else:
            lines = [VoucherLineDTO(ACC_ALLOWANCE, "DEBIT", -delta),
                     VoucherLineDTO(ACC_BADDEBT, "CREDIT", -delta)]
        v = await self._vs.create(
            session, source_module="closing-ifrs", source_ref=f"allowance:AR:{yyyymm}",
            voucher_on=until, lines=lines, memo="대손충당금 조정(K-IFRS 1109)",
            created_by=actor, allow_closed=True)
        return {"posted": True, "voucher_id": v.id, **p}
