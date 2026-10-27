"""복리후생 (Tier3·0230) — 제도·연 부여·신청/승인/지급(잔액 차감·한도 검증·지급 GL).

잔액 = 부여액(BenefitGrant.granted·없으면 제도 annual_limit) − 승인/지급 사용액(당해연도).
지급(pay) 시 DR 5330 복리후생비 / CR 1110 현금 전표(멱등). finance import 허용(hr→finance).
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import BenefitClaim, BenefitGrant, BenefitProgram, Employee

_audit = AuditService()
_ACC_WELFARE = "5330"   # 복리후생비
_ACC_CASH = "1110"      # 현금
_USED_STATES = ("APPROVED", "PAID")


class WelfareService:
    # ── 제도 ────────────────────────────────────────────────
    async def create_program(self, session: AsyncSession, *, actor: int, code: str, title: str,
                             benefit_type: str = "POINT", unit: str = "KRW",
                             annual_limit: float = 0, target: str = "ALL",
                             target_value: str | None = None, requires_receipt: bool = True,
                             note: str | None = None) -> BenefitProgram:
        if not code.strip() or not title.strip():
            raise ValidationError("제도 코드·명칭은 필수입니다")
        if annual_limit < 0:
            raise ValidationError("연 한도는 0 이상이어야 합니다")
        if target in ("JOB_CATEGORY", "DEPT") and not (target_value or "").strip():
            raise ValidationError("대상 한정 시 대상값이 필요합니다")
        if await session.scalar(select(BenefitProgram.id).where(BenefitProgram.code == code)):
            raise ValidationError(f"이미 존재하는 제도 코드: {code}")
        p = BenefitProgram(code=code.strip()[:30], title=title.strip()[:160],
                           benefit_type=benefit_type, unit=unit, annual_limit=annual_limit,
                           target=target, target_value=(target_value or None),
                           requires_receipt=requires_receipt, note=note, created_by=actor)
        session.add(p)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="hr.benefit_programs", target_id=str(p.id),
                                  after={"code": code})
        return p

    async def list_programs(self, session: AsyncSession, *, active: bool | None = None) -> list[dict]:
        q = select(BenefitProgram)
        if active is not None:
            q = q.where(BenefitProgram.active.is_(active))
        rows = (await session.scalars(q.order_by(BenefitProgram.code))).all()
        return [{"id": p.id, "code": p.code, "title": p.title, "benefit_type": p.benefit_type,
                 "unit": p.unit, "annual_limit": float(p.annual_limit), "target": p.target,
                 "target_value": p.target_value, "requires_receipt": p.requires_receipt,
                 "active": p.active, "note": p.note} for p in rows]

    async def set_program_active(self, session: AsyncSession, *, actor: int, program_id: int,
                                 active: bool) -> BenefitProgram:
        p = await session.get(BenefitProgram, program_id)
        if p is None:
            raise ValidationError("존재하지 않는 제도입니다")
        p.active = active
        return p

    # ── 부여 ────────────────────────────────────────────────
    async def grant(self, session: AsyncSession, *, actor: int, program_id: int,
                    employee_id: int, year: int, granted: float,
                    note: str | None = None) -> BenefitGrant:
        if await session.get(BenefitProgram, program_id) is None:
            raise ValidationError("존재하지 않는 제도입니다")
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if granted < 0:
            raise ValidationError("부여액은 0 이상이어야 합니다")
        row = await session.scalar(select(BenefitGrant).where(
            BenefitGrant.program_id == program_id, BenefitGrant.employee_id == employee_id,
            BenefitGrant.year == year))
        if row is None:
            row = BenefitGrant(program_id=program_id, employee_id=employee_id, year=year,
                               granted=granted, note=note, created_by=actor)
            session.add(row)
        else:
            row.granted = granted
        await session.flush()
        return row

    async def grant_bulk(self, session: AsyncSession, *, actor: int, program_id: int, year: int,
                         granted: float, employee_ids: list[int]) -> dict:
        for eid in employee_ids:
            await self.grant(session, actor=actor, program_id=program_id, employee_id=eid,
                             year=year, granted=granted)
        return {"program_id": program_id, "year": year, "count": len(employee_ids),
                "granted": granted}

    # ── 잔액 ────────────────────────────────────────────────
    async def _limit(self, session: AsyncSession, program: BenefitProgram, employee_id: int,
                     year: int) -> float:
        g = await session.scalar(select(BenefitGrant.granted).where(
            BenefitGrant.program_id == program.id, BenefitGrant.employee_id == employee_id,
            BenefitGrant.year == year))
        return float(g) if g is not None else float(program.annual_limit)

    async def _used(self, session: AsyncSession, program_id: int, employee_id: int,
                    year: int, *, exclude_claim: int | None = None) -> float:
        q = select(func.coalesce(func.sum(BenefitClaim.amount), 0)).where(
            BenefitClaim.program_id == program_id, BenefitClaim.employee_id == employee_id,
            BenefitClaim.year == year, BenefitClaim.status.in_(_USED_STATES))
        if exclude_claim is not None:
            q = q.where(BenefitClaim.id != exclude_claim)
        return float(await session.scalar(q) or 0)

    async def balance(self, session: AsyncSession, *, employee_id: int,
                      year: int) -> list[dict]:
        """직원 대상 제도별 한도·사용·잔액."""
        emp = await session.get(Employee, employee_id)
        progs = (await session.scalars(select(BenefitProgram).where(
            BenefitProgram.active.is_(True)).order_by(BenefitProgram.code))).all()
        out = []
        for p in progs:
            if p.target == "JOB_CATEGORY" and (emp is None
                                               or (emp.job_category or "") != (p.target_value or "")):
                continue
            if p.target == "DEPT" and (emp is None
                                       or str(emp.department_id or "") != (p.target_value or "")):
                continue
            limit = await self._limit(session, p, employee_id, year)
            used = await self._used(session, p.id, employee_id, year)
            out.append({"program_id": p.id, "code": p.code, "title": p.title, "unit": p.unit,
                        "limit": limit, "used": used, "remaining": limit - used})
        return out

    # ── 신청·승인·지급 ──────────────────────────────────────
    async def claim(self, session: AsyncSession, *, actor: int, program_id: int, employee_id: int,
                    amount: float, claim_on: date, reason: str | None = None) -> BenefitClaim:
        p = await session.get(BenefitProgram, program_id)
        if p is None or not p.active:
            raise ValidationError("유효하지 않은 제도입니다")
        if amount <= 0:
            raise ValidationError("신청 금액은 0보다 커야 합니다")
        year = claim_on.year
        limit = await self._limit(session, p, employee_id, year)
        used = await self._used(session, program_id, employee_id, year)
        if used + amount > limit:
            raise ValidationError(
                f"연 한도 초과 — 한도 {int(limit):,} · 사용 {int(used):,} · 신청 {int(amount):,}",
                {"limit": limit, "used": used})
        c = BenefitClaim(program_id=program_id, employee_id=employee_id, year=year, amount=amount,
                         claim_on=claim_on, reason=reason, status="REQUESTED", created_by=actor)
        session.add(c)
        await session.flush()
        return c

    async def _get_claim(self, session, claim_id: int, *, lock: bool = False) -> BenefitClaim:
        c = await session.get(BenefitClaim, claim_id, with_for_update=lock)
        if c is None:
            raise ValidationError("존재하지 않는 신청입니다")
        return c

    async def decide(self, session: AsyncSession, *, actor: int, claim_id: int, approve: bool,
                     ) -> BenefitClaim:
        c = await self._get_claim(session, claim_id, lock=True)
        if c.status != "REQUESTED":
            raise ValidationError("처리 가능한 상태가 아닙니다", {"status": c.status})
        if approve:
            # 승인 시점 한도 재검증(동시 신청 경합) — 본 신청 제외 사용액 + 금액 ≤ 한도
            p = await session.get(BenefitProgram, c.program_id)
            limit = await self._limit(session, p, c.employee_id, c.year)
            used = await self._used(session, c.program_id, c.employee_id, c.year,
                                    exclude_claim=c.id)
            if used + float(c.amount) > limit:
                raise ValidationError("승인 시 한도 초과 — 다른 신청 승인으로 잔액 소진")
            c.status = "APPROVED"
        else:
            c.status = "REJECTED"
        c.decided_by = actor
        await _audit.record_audit(session, actor=actor, action="APPROVE" if approve else "REJECT",
                                  entity="hr.benefit_claims", target_id=str(c.id),
                                  after={"status": c.status})
        return c

    async def pay(self, session: AsyncSession, *, actor: int, claim_id: int,
                  paid_on: date | None = None) -> BenefitClaim:
        """지급 — DR 5330 복리후생비 / CR 1110 현금. 멱등(claim id 참조)."""
        from erp.finance.service import VoucherLineDTO, VoucherService
        c = await self._get_claim(session, claim_id, lock=True)
        if c.status != "APPROVED":
            raise ValidationError("승인된 신청만 지급할 수 있습니다", {"status": c.status})
        on = paid_on or date.today()
        amt = round(float(c.amount))
        v = await VoucherService().create(
            session, source_module="hr.welfare", source_ref=f"welfare:{c.id}",
            voucher_on=on, created_by=actor, memo=f"복리후생 지급 #{c.id}",
            lines=[VoucherLineDTO(_ACC_WELFARE, "DEBIT", amt),
                   VoucherLineDTO(_ACC_CASH, "CREDIT", amt)])
        c.status, c.paid_on, c.voucher_ref = "PAID", on, f"voucher-{v.id}"
        await _audit.record_audit(session, actor=actor, action="PAY",
                                  entity="hr.benefit_claims", target_id=str(c.id),
                                  after={"paid_on": str(on), "amount": amt})
        return c

    async def list_claims(self, session: AsyncSession, *, status: str | None = None,
                          employee_id: int | None = None, year: int | None = None) -> list[dict]:
        q = (select(BenefitClaim, BenefitProgram.title, Employee.name, Employee.employee_no)
             .join(BenefitProgram, BenefitProgram.id == BenefitClaim.program_id)
             .join(Employee, Employee.id == BenefitClaim.employee_id))
        if status:
            q = q.where(BenefitClaim.status == status)
        if employee_id is not None:
            q = q.where(BenefitClaim.employee_id == employee_id)
        if year is not None:
            q = q.where(BenefitClaim.year == year)
        rows = (await session.execute(q.order_by(BenefitClaim.id.desc()).limit(500))).all()
        return [{"id": c.id, "program_id": c.program_id, "program_title": title,
                 "employee_id": c.employee_id, "employee_no": no, "name": name,
                 "amount": float(c.amount), "claim_on": c.claim_on, "reason": c.reason,
                 "status": c.status, "paid_on": c.paid_on, "voucher_ref": c.voucher_ref}
                for c, title, name, no in rows]
