"""이연법인세 (K-IFRS 1012) — 일시적차이 × 세율 = DTA/DTL.

ERP가 자동 식별 가능한 **P/L-origin 차감할 일시적차이**(퇴직충당·대손·재고평가 충당)의 장부 잔액에
유효세율을 곱해 이연법인세자산(DTA)을 측정한다. 연 1회(연말) 측정·차액 결산분개(법인세비용 5810).
**한계(범위 외)**: ① 당기법인세(과세소득×세율)·세무조정·이월결손 ② OCI-origin 항목(재평가잉여금·손상 OCI분·
보험수리적손익)의 이연법인세 — IAS 12.61A backwards tracing 필요 ③ 회계충당=세무부인액 가정(세무조정 명세 별도).
모두 세무 전문 검증 권장.
"""
from datetime import date

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.fiscal import fiscal_range
from erp.finance.models import TaxRateSetting, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_DTA = "1180"        # 이연법인세자산
ACC_DTL = "2260"        # 이연법인세부채
ACC_TAX_EXPENSE = "5810"  # 법인세비용
_DEFAULT_RATE = 0.20

# 일시적차이 원천(P/L-origin 차감할 일시적차이만) — 충당/대손/재고평가. 잔액은 대변기준.
# ⚠️ OCI-origin 항목(재평가잉여금 3400·손상누계 1815의 재평가분·보험수리적손익 3410)은 IAS 12.61A
#    backwards tracing(세효과를 OCI에 귀속)이 필요하므로 본 엔진에서 제외 — 세무 전문 처리(범위 외).
TEMP_DIFF_SOURCES = [
    ("2120", "DTA", "퇴직급여충당부채"),
    ("1190", "DTA", "대손충당금"),
    ("1159", "DTA", "재고자산평가충당금"),
]


class DeferredTaxService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def rate(self, session: AsyncSession, *, as_of: date) -> float:
        r = (await session.execute(
            select(TaxRateSetting.rate).where(TaxRateSetting.valid_from <= as_of)
            .order_by(TaxRateSetting.valid_from.desc()).limit(1))).scalar()
        return float(r) if r is not None else _DEFAULT_RATE

    async def _cr_balance(self, session: AsyncSession, *, as_of: date, code: str) -> float:
        """단일 계정 대변잔액(cr-d) — _cr_balances 위 얇은 래퍼."""
        return (await self._cr_balances(session, as_of=as_of, codes=[code])).get(code, 0.0)

    async def _cr_balances(self, session: AsyncSession, *, as_of: date,
                           codes: list[str]) -> dict[str, float]:
        """여러 계정의 대변잔액(cr-d)을 1쿼리로 일괄 산정(N+1 제거). 미존재 계정은 호출부 .get(.,0)."""
        rows = (await session.execute(
            select(
                VoucherLine.account_code,
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0)
                - func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code.in_(codes), Voucher.voucher_on <= as_of)
            .group_by(VoucherLine.account_code))).all()
        return {code: float(bal) for code, bal in rows}

    async def preview(self, session: AsyncSession, *, year: int) -> dict:
        """연말 일시적차이·DTA/DTL·조정차액 미리보기."""
        as_of = (await fiscal_range(session, year))[1]   # 기관 회계연도 말일
        rate = await self.rate(session, as_of=as_of)
        # 일시적차이 원천 + 현재 DTA/DTL 잔액을 1쿼리 일괄(과거 5회 개별 집계 → 1회).
        bals = await self._cr_balances(
            session, as_of=as_of, codes=[c for c, _, _ in TEMP_DIFF_SOURCES] + [ACC_DTA, ACC_DTL])
        items, dta_base, dtl_base = [], 0.0, 0.0
        for code, nature, name in TEMP_DIFF_SOURCES:
            bal = bals.get(code, 0.0)
            if bal == 0:
                continue
            items.append({"account": code, "name": name, "nature": nature,
                          "temp_diff": round(bal), "tax_effect": round(bal * rate)})
            if nature == "DTA":
                dta_base += bal
            else:
                dtl_base += bal
        dta = round(dta_base * rate)
        dtl = round(dtl_base * rate)
        cur_dta = round(-bals.get(ACC_DTA, 0.0))  # 자산=차변
        cur_dtl = round(bals.get(ACC_DTL, 0.0))    # 부채=대변
        return {"year": year, "as_of": str(as_of), "rate": rate, "items": items,
                "dta": dta, "dtl": dtl, "current_dta": cur_dta, "current_dtl": cur_dtl,
                "delta_dta": dta - cur_dta, "delta_dtl": dtl - cur_dtl}

    async def post(self, session: AsyncSession, *, year: int, actor: int | None = None) -> dict:
        """이연법인세 차액 결산분개(멱등). DTA 증가 1180/5810·DTL 증가 5810/2260."""
        p = await self.preview(session, year=year)
        d_dta, d_dtl = p["delta_dta"], p["delta_dtl"]
        if d_dta == 0 and d_dtl == 0:
            return {"posted": False, "reason": "조정 없음", **p}
        lines = []
        if d_dta > 0:
            lines += [VoucherLineDTO(ACC_DTA, "DEBIT", d_dta),
                      VoucherLineDTO(ACC_TAX_EXPENSE, "CREDIT", d_dta)]
        elif d_dta < 0:
            lines += [VoucherLineDTO(ACC_TAX_EXPENSE, "DEBIT", -d_dta),
                      VoucherLineDTO(ACC_DTA, "CREDIT", -d_dta)]
        if d_dtl > 0:
            lines += [VoucherLineDTO(ACC_TAX_EXPENSE, "DEBIT", d_dtl),
                      VoucherLineDTO(ACC_DTL, "CREDIT", d_dtl)]
        elif d_dtl < 0:
            lines += [VoucherLineDTO(ACC_DTL, "DEBIT", -d_dtl),
                      VoucherLineDTO(ACC_TAX_EXPENSE, "CREDIT", -d_dtl)]
        v = await self._vs.create(
            session, source_module="closing-ifrs", source_ref=f"deferred-tax:{year}",
            voucher_on=(await fiscal_range(session, year))[1], lines=lines,
            memo="이연법인세 조정(K-IFRS 1012)", created_by=actor, allow_closed=True)
        return {"posted": True, "voucher_id": v.id, **p}
