"""변동대가 — 보험삭감 추정 (K-IFRS 1115).

미결정 청구(제출됐으나 미결정)는 과거 삭감률로 예상 삭감을 추정해 수익을 순액(예상 결정액)으로
인식한다. 예상 삭감분은 수익 차감 + 환불부채(2155). 실제 결정 시 정산. ERP claims 데이터로 자체 산출.

미결정 풀 = **제출(ClaimSubmission) − 결정(decided_on)**. 결정 신호는 실제 삭감(claim.reviewed·5910)과
동일하게 정렬돼 추정과 실적이 겹치지 않는다. decided_on 기준 as-of 재구성으로 마감월 재실행 오조정 방지.
"""
from calendar import monthrange
from datetime import date

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.models import ClaimStatement, ClaimSubmission

ACC_REVENUE = "4110"        # 의료수익(차감)
ACC_REFUND_LIAB = "2155"    # 환불부채(변동대가)


def _month_end(yyyymm: str) -> date:
    y, m = int(yyyymm[:4]), int(yyyymm[5:7])
    return date(y, m, monthrange(y, m)[1])


class VariableConsiderationService:
    async def deduction_rates(self, session: AsyncSession) -> dict:
        """과거 삭감률 — 결정 완료 명세 기준 진료과별·전체 Σ(청구−결정)/Σ청구."""
        rows = (await session.execute(
            select(ClaimStatement.dept_code,
                   func.coalesce(func.sum(ClaimStatement.claimed_amt), 0),
                   func.coalesce(func.sum(ClaimStatement.claimed_amt
                                          - ClaimStatement.decided_amt), 0))
            .where(ClaimStatement.decided_amt.isnot(None),
                   ClaimStatement.claimed_amt > 0)
            .group_by(ClaimStatement.dept_code))).all()
        by_dept, tot_claim, tot_ded = {}, 0.0, 0.0
        for dc, claim, ded in rows:
            claim, ded = float(claim), float(ded)
            tot_claim += claim
            tot_ded += ded
            if claim > 0:
                # 삭감률은 [0,1]로 클램프 — 결정액>청구액(가산·이자) 진료과의 음수율이 미결정 청구에
                # 곱해져 예상삭감·환불부채가 음수화(부채 차변)되는 것을, 초과율이 예상삭감을 청구액
                # 이상으로 부풀리는 것을 방지.
                by_dept[dc] = round(max(0.0, min(ded / claim, 1.0)), 4)
        overall = round(max(0.0, min(tot_ded / tot_claim, 1.0)), 4) if tot_claim else 0.0
        return {"overall": overall, "by_dept": by_dept}

    async def preview(self, session: AsyncSession, *, yyyymm: str) -> dict:
        """미결정 청구 예상 삭감(변동대가) — 제출−결정 풀 × 전체 삭감률, 환불부채 차액.

        미결정 풀 = 제출(ClaimSubmission) 중 **월말 기준 미결정**(decided_on IS NULL OR > 월말).
        결정 신호가 실제 삭감(claim.reviewed)과 정렬돼 이중차감 없음. care_month<=yyyymm 누적
        (환불부채 2155는 단일 누적 잔액이므로 expected도 누적). 제출은 dept 미보유라 전체 삭감률 적용.
        care_month 미동봉(NULL) 제출은 월 귀속 불가라 제외(대사불가로 별도 표면화).
        """
        rates = await self.deduction_rates(session)
        rate = rates["overall"]
        asof = _month_end(yyyymm)
        undecided = float(await session.scalar(
            select(func.coalesce(func.sum(ClaimSubmission.claimed_amt), 0))
            .where(ClaimSubmission.care_month.isnot(None),
                   ClaimSubmission.care_month <= yyyymm,
                   ClaimSubmission.claimed_amt > 0,
                   # 제출 as-of 가드 — 월말 후 접수된 제출은 그 월 as-of 풀에서 제외(과거월
                   # 재실행 시 미래 제출 누출 방지). submitted_on 미동봉(NULL)은 판정 불가라 포함.
                   or_(ClaimSubmission.submitted_on.is_(None),
                       ClaimSubmission.submitted_on <= asof),
                   or_(ClaimSubmission.decided_on.is_(None),
                       ClaimSubmission.decided_on > asof))) or 0)
        expected = round(undecided * rate)
        current = await self._refund_balance(session, as_of=asof)
        return {"yyyymm": yyyymm, "submitted_undecided": round(undecided), "rate": rate,
                "items": [{"basis": "submitted_undecided", "claimed": round(undecided),
                           "rate": rate, "expected_deduction": expected}],
                "expected_deduction": expected,
                "current": round(current), "delta": expected - round(current)}

    async def _refund_balance(self, session: AsyncSession, *, as_of: date) -> float:
        from erp.finance.models import Voucher, VoucherLine
        d, cr = (await session.execute(
            select(
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                func.coalesce(func.sum(case(
                    (VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .where(VoucherLine.account_code == ACC_REFUND_LIAB,
                   Voucher.voucher_on <= as_of))).one()
        return float(cr) - float(d)         # 환불부채=대변잔액

    async def post(self, session: AsyncSession, *, yyyymm: str,
                   actor: int | None = None) -> dict:
        """변동대가 차액 결산분개 — 증가 4110/2155(수익 차감)·감소 반대.

        각 전기는 **잔액 대비 증분(delta)**만 올린다. 미결정 풀이 월중/월후 계속 커지므로(제출
        연속 캡처) 같은 월을 여러 번 post하면 매번 새 증분이 생긴다. source_ref는 월 고정이 아니라
        posting별 순번(vN)으로 유니크화 — 월 단일키였을 때 VoucherService (module,ref) 멱등에 걸려
        2번째 이후 증분이 전기 안 되면서 posted:True만 반환하던 결함 방지(#627 동형). 증분 0이면 no-op.
        """
        from erp.finance.models import Voucher
        from erp.finance.service import VoucherLineDTO, VoucherService
        p = await self.preview(session, yyyymm=yyyymm)
        delta = p["delta"]
        if delta == 0:
            return {"posted": False, "reason": "조정 없음", **p}
        if delta > 0:        # 예상삭감 증가 → 수익 차감 / 환불부채
            lines = [VoucherLineDTO(ACC_REVENUE, "DEBIT", delta),
                     VoucherLineDTO(ACC_REFUND_LIAB, "CREDIT", delta)]
        else:
            lines = [VoucherLineDTO(ACC_REFUND_LIAB, "DEBIT", -delta),
                     VoucherLineDTO(ACC_REVENUE, "CREDIT", -delta)]
        if session.bind.dialect.name == "postgresql":   # :vN 채번 직렬화(동시 post uq 충돌 방지)
            await session.execute(
                select(func.pg_advisory_xact_lock(0x56415243, func.hashtext(yyyymm))))  # 'VARC'
        seq = int(await session.scalar(
            select(func.count(Voucher.id)).where(
                Voucher.source_module == "closing-ifrs",
                Voucher.source_ref.like(f"var-consideration:{yyyymm}:v%"))) or 0)
        v = await VoucherService().create(
            session, source_module="closing-ifrs",
            source_ref=f"var-consideration:{yyyymm}:v{seq + 1}",
            voucher_on=_month_end(yyyymm), lines=lines,
            memo="변동대가(보험삭감 추정, K-IFRS 1115)", created_by=actor, allow_closed=True)
        return {"posted": True, "voucher_id": v.id, **p}
