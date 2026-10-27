"""예산 통제 — 약정(encumbrance)·예산 변경 이력. 3-way: 예산 vs 약정 vs 집행 → 가용.

예실대비/약정 집계는 budget_execution(report_service)에서 합산. 본 서비스는 약정 생성·해제,
예산 변경(이력 기록)을 담당.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import Budget, BudgetAmendment, Encumbrance
from erp.common.kst import kst_iso


class BudgetService:
    async def encumber(self, session: AsyncSession, *, department_id: int, fiscal_year: int,
                       account_code: str, amount: float, description: str = "",
                       source_ref: str | None = None, user_id: int | None = None) -> Encumbrance:
        """예산 약정 — 지출예정 예약(발주승인 등). source_ref로 멱등."""
        if amount <= 0:
            raise ValidationError("약정 금액은 0보다 커야 합니다")
        if source_ref:
            dup = await session.scalar(select(Encumbrance).where(Encumbrance.source_ref == source_ref))
            if dup is not None:
                return dup
        enc = Encumbrance(department_id=department_id, fiscal_year=fiscal_year,
                          account_code=account_code, amount=amount, description=description or None,
                          source_ref=source_ref, status="OPEN", created_by=user_id)
        session.add(enc)
        return enc

    async def release(self, session: AsyncSession, *, encumbrance_id: int) -> Encumbrance:
        """약정 해제 — 집행 완료/취소 시 가용예산 복원."""
        enc = await session.get(Encumbrance, encumbrance_id)
        if enc is None:
            raise ValidationError("존재하지 않는 약정입니다")
        if enc.status == "RELEASED":
            return enc
        enc.status = "RELEASED"
        enc.released_at = datetime.now(timezone.utc)
        return enc

    async def list_encumbrances(self, session: AsyncSession, *, fiscal_year: int,
                                department_id: int | None = None,
                                status: str = "OPEN") -> list[dict]:
        """약정 목록 — 해제 대상 선택용(FR-09 화면). status=OPEN 기본(가용예산 차감 중)."""
        q = select(Encumbrance).where(Encumbrance.fiscal_year == fiscal_year)
        if department_id is not None:
            q = q.where(Encumbrance.department_id == department_id)
        if status:
            q = q.where(Encumbrance.status == status)
        rows = (await session.execute(q.order_by(Encumbrance.id.desc()).limit(500))).scalars().all()
        return [{"id": e.id, "department_id": e.department_id, "account_code": e.account_code,
                 "amount": float(e.amount), "description": e.description,
                 "source_ref": e.source_ref, "status": e.status,
                 "created_at": kst_iso(e.created_at)}
                for e in rows]

    async def amend(self, session: AsyncSession, *, department_id: int, fiscal_year: int,
                    account_code: str, new_amount: float, reason: str = "",
                    user_id: int | None = None) -> dict:
        """예산 증액/감액 — 변경 이력 기록 후 적용(upsert).

        기존 행은 잠금(with_for_update)으로 동시 증감의 lost-update 방지. 신규는 유니크
        제약이 동시 중복삽입을 차단(sqlite는 잠금 무시).
        """
        budget = await session.scalar(select(Budget).where(
            Budget.department_id == department_id, Budget.fiscal_year == fiscal_year,
            Budget.account_code == account_code).with_for_update())
        old = float(budget.amount) if budget else None
        session.add(BudgetAmendment(department_id=department_id, fiscal_year=fiscal_year,
                                    account_code=account_code, old_amount=old,
                                    new_amount=new_amount, reason=reason or None, changed_by=user_id))
        if budget is None:
            budget = Budget(department_id=department_id, fiscal_year=fiscal_year,
                            account_code=account_code, amount=new_amount)
            session.add(budget)
        else:
            budget.amount = new_amount
        return {"department_id": department_id, "account_code": account_code,
                "old_amount": old, "new_amount": new_amount}

    async def transfer(self, session: AsyncSession, *, department_id: int, fiscal_year: int,
                       from_account: str, to_account: str, amount: float, reason: str = "",
                       user_id: int | None = None) -> dict:
        """예산 전용(轉用) — 과목 간 이전(감액·증액 짝). 원 과목 가용 검증·이력 2건."""
        if amount <= 0:
            raise ValidationError("전용액은 0보다 커야 합니다")
        if from_account == to_account:
            raise ValidationError("동일 과목 간 전용은 불가합니다")
        src = await session.scalar(select(Budget).where(
            Budget.department_id == department_id, Budget.fiscal_year == fiscal_year,
            Budget.account_code == from_account).with_for_update())
        if src is None or float(src.amount) < amount:
            raise ValidationError("전용 가능 예산이 부족합니다",
                                  {"available": float(src.amount) if src else 0})
        dst = await session.scalar(select(Budget).where(
            Budget.department_id == department_id, Budget.fiscal_year == fiscal_year,
            Budget.account_code == to_account))
        dst_amt = float(dst.amount) if dst else 0.0
        await self.amend(session, department_id=department_id, fiscal_year=fiscal_year,
                         account_code=from_account, new_amount=float(src.amount) - amount,
                         reason=f"전용→{to_account}: {reason}", user_id=user_id)
        await self.amend(session, department_id=department_id, fiscal_year=fiscal_year,
                         account_code=to_account, new_amount=dst_amt + amount,
                         reason=f"전용←{from_account}: {reason}", user_id=user_id)
        return {"department_id": department_id, "fiscal_year": fiscal_year,
                "from": from_account, "to": to_account, "amount": round(amount)}

    async def carry_over(self, session: AsyncSession, *, department_id: int, from_year: int,
                         user_id: int | None = None) -> dict:
        """예산 이월 — 전년 미집행 잔액(예산−집행)을 차년도 예산으로. 차년도 기존 과목은 건너뜀(멱등)."""
        from erp.finance.models import Voucher, VoucherLine
        to_year = from_year + 1
        budgets = (await session.execute(select(Budget).where(
            Budget.department_id == department_id, Budget.fiscal_year == from_year))).scalars().all()
        carried = []
        for b in budgets:
            executed = float((await session.execute(
                select(func.coalesce(func.sum(VoucherLine.amount), 0))
                .join(Voucher, Voucher.id == VoucherLine.voucher_id)
                .where(VoucherLine.account_code == b.account_code,
                       VoucherLine.department_id == department_id,
                       func.extract("year", Voucher.voucher_on) == from_year,
                       Voucher.status == "POSTED",   # 역분개된 지출 제외(REVERSED 원전표 미포함=집행 과대·이월 과소 방지)
                       VoucherLine.side == "DEBIT"))).scalar() or 0)
            unused = round(float(b.amount) - executed)
            if unused <= 0:
                continue
            exists = await session.scalar(select(Budget).where(
                Budget.department_id == department_id, Budget.fiscal_year == to_year,
                Budget.account_code == b.account_code))
            if exists is not None:   # 차년도 이미 편성 — 이월 중복 방지(멱등)
                continue
            await self.amend(session, department_id=department_id, fiscal_year=to_year,
                             account_code=b.account_code, new_amount=unused,
                             reason=f"이월:{from_year}", user_id=user_id)
            carried.append({"account_code": b.account_code, "amount": unused})
        return {"department_id": department_id, "from_year": from_year, "to_year": to_year,
                "carried": carried, "total": sum(c["amount"] for c in carried)}

    async def amendments(self, session: AsyncSession, *, fiscal_year: int,
                         department_id: int | None = None) -> list[dict]:
        q = select(BudgetAmendment).where(BudgetAmendment.fiscal_year == fiscal_year)
        if department_id is not None:
            q = q.where(BudgetAmendment.department_id == department_id)
        rows = (await session.execute(q.order_by(BudgetAmendment.id.desc()))).scalars().all()
        return [{"department_id": a.department_id, "account_code": a.account_code,
                 "old_amount": float(a.old_amount) if a.old_amount is not None else None,
                 "new_amount": float(a.new_amount), "reason": a.reason,
                 "changed_at": a.changed_at.isoformat()} for a in rows]
