"""지출·경비 관리 — 경비청구(기안→결재선→승인전기→지급)·법인카드(적재→연결→월정산) (Tier1·0210).

회계:
  승인 시  DR 비용계정(라인별·부서 차원) / CR 2110 미지급금(개인분)·2112 법인카드미지급금(카드분)
  지급 시  DR 2110 / CR 1110 — 개인분만(카드분은 카드사 월정산 settle_card_month가 DR 2112/CR 1110)
결재선 = ApprovalEngine(doc_type="EXPENSE") 재사용 — 룰 없으면 단일승인 폴백(직무분리 SoD 유지).
법인카드 사용내역은 카드사 승인번호(tx_id) 멱등 적재·경비 라인 연결 시 CLAIMED·정산 시 SETTLED.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.approval_engine import ApprovalEngine
from erp.errors import ValidationError
from erp.finance.models import Account, CardTransaction, CorporateCard, ExpenseClaim, ExpenseLine
from erp.finance.service import VoucherLineDTO, VoucherService

_DOC = "EXPENSE"
ACC_CASH = "1110"
ACC_PAYABLE = "2110"        # 미지급금(개인 경비)
ACC_CARD_PAYABLE = "2112"   # 법인카드미지급금(카드사 월정산 대사)


class ExpenseService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vs = vouchers or VoucherService()
        self._approval = ApprovalEngine()

    # ── 경비 청구 ───────────────────────────────────────────
    async def create_claim(self, session: AsyncSession, *, employee_id: int,
                           department_id: int | None, title: str, claim_on: date,
                           lines: list[dict], note: str | None = None,
                           created_by: int) -> ExpenseClaim:
        """기안(DRAFT) — 라인 검증(비용계정·금액>0)·법인카드 내역 연결(금액 일치·UNMATCHED만)."""
        if not lines:
            raise ValidationError("경비 라인이 없습니다")
        codes = {ln["account_code"] for ln in lines}
        rows = (await session.execute(
            select(Account.code, Account.kind).where(Account.code.in_(codes)))).all()
        kinds = {c: k for c, k in rows}
        for c in codes:
            if kinds.get(c) != "EXPENSE":
                raise ValidationError(f"비용 계정이 아닙니다: {c}", {"kind": kinds.get(c)})
        claim = ExpenseClaim(employee_id=employee_id, department_id=department_id,
                             title=title, note=note, claim_on=claim_on,
                             created_by=created_by)
        session.add(claim)
        await session.flush()
        total = 0.0
        for ln in lines:
            amt = round(float(ln["amount"]))
            if amt <= 0:
                raise ValidationError("경비 금액은 0보다 커야 합니다")
            tx_id = ln.get("card_tx_id")
            if tx_id is not None:
                tx = await session.get(CardTransaction, int(tx_id))
                if tx is None:
                    raise ValidationError("존재하지 않는 카드 사용내역입니다", {"card_tx_id": tx_id})
                if tx.status != "UNMATCHED":
                    raise ValidationError("이미 연결/정산된 카드 사용내역입니다",
                                          {"status": tx.status})
                if round(float(tx.amount)) != amt:
                    raise ValidationError("카드 사용내역 금액과 라인 금액이 다릅니다",
                                          {"tx": float(tx.amount), "line": amt})
                tx.status, tx.claim_id = "CLAIMED", claim.id
            session.add(ExpenseLine(claim_id=claim.id, expense_on=ln["expense_on"],
                                    account_code=ln["account_code"],
                                    description=ln["description"],
                                    merchant=ln.get("merchant"), amount=amt,
                                    card_tx_id=tx_id))
            total += amt
        claim.total_amount = total
        await session.flush()
        return claim

    async def _get(self, session: AsyncSession, claim_id: int) -> ExpenseClaim:
        c = await session.get(ExpenseClaim, claim_id)
        if c is None:
            raise ValidationError("존재하지 않는 경비 청구입니다", {"claim_id": claim_id})
        return c

    async def _lines(self, session: AsyncSession, claim_id: int) -> list[ExpenseLine]:
        return list((await session.scalars(
            select(ExpenseLine).where(ExpenseLine.claim_id == claim_id)
            .order_by(ExpenseLine.id))).all())

    async def submit(self, session: AsyncSession, *, claim_id: int, actor: int) -> ExpenseClaim:
        """상신 — DRAFT→PENDING·결재선 생성(룰 있으면). 룰 없으면 단일승인 폴백."""
        c = await self._get(session, claim_id)
        if c.status != "DRAFT":
            raise ValidationError("상신 가능한 상태가 아닙니다", {"status": c.status})
        c.status = "PENDING"
        await self._approval.build(session, doc_type=_DOC, doc_id=claim_id,
                                   amount=float(c.total_amount), created_by=actor,
                                   department_id=c.department_id)
        return c

    async def approve(self, session: AsyncSession, *, claim_id: int, actor: int,
                      override: bool = False) -> ExpenseClaim:
        """승인 — 결재선 완주(또는 룰 없음 단일승인·SoD) 시 비용/미지급 전기·APPROVED."""
        c = await self._get(session, claim_id)
        if c.status != "PENDING":
            raise ValidationError("승인 가능한 상태가 아닙니다", {"status": c.status})
        inst = await self._approval.get(session, doc_type=_DOC, doc_id=claim_id)
        if inst is None:                       # 룰 없음 → 단일 승인(직무분리)
            if c.created_by == actor:
                raise ValidationError("직무분리 — 기안자 본인은 승인할 수 없습니다")
            return await self._finalize(session, c, actor)
        out = await self._approval.act(session, doc_type=_DOC, doc_id=claim_id, actor=actor,
                                       approve=True, override=override)
        if out.status == "APPROVED":
            return await self._finalize(session, c, actor)
        return c                                # 다음 단계 대기

    async def reject(self, session: AsyncSession, *, claim_id: int, actor: int,
                     reason: str = "") -> ExpenseClaim:
        c = await self._get(session, claim_id)
        if c.status != "PENDING":
            raise ValidationError("반려 가능한 상태가 아닙니다", {"status": c.status})
        inst = await self._approval.get(session, doc_type=_DOC, doc_id=claim_id)
        if inst is not None and inst.get("status") == "PENDING":
            await self._approval.act(session, doc_type=_DOC, doc_id=claim_id, actor=actor,
                                     approve=False, note=reason)
        c.status = "REJECTED"
        # 연결 카드내역 원복(재청구 가능)
        for tx in (await session.scalars(select(CardTransaction).where(
                CardTransaction.claim_id == claim_id))).all():
            tx.status, tx.claim_id = "UNMATCHED", None
        return c

    async def _finalize(self, session: AsyncSession, c: ExpenseClaim,
                        actor: int) -> ExpenseClaim:
        """승인 확정 — DR 비용(계정별 합산·부서 차원) / CR 2110(개인분)+2112(카드분)."""
        lines = await self._lines(session, c.id)
        by_acct: dict[str, float] = {}
        personal = card = 0.0
        for ln in lines:
            amt = float(ln.amount)
            by_acct[ln.account_code] = by_acct.get(ln.account_code, 0.0) + amt
            if ln.card_tx_id is not None:
                card += amt
            else:
                personal += amt
        vlines = [VoucherLineDTO(acct, "DEBIT", amt, department_id=c.department_id)
                  for acct, amt in sorted(by_acct.items())]
        if personal > 0:
            vlines.append(VoucherLineDTO(ACC_PAYABLE, "CREDIT", personal))
        if card > 0:
            vlines.append(VoucherLineDTO(ACC_CARD_PAYABLE, "CREDIT", card))
        v = await self._vs.create(session, source_module="expense",
                                  source_ref=f"expense:{c.id}", voucher_on=c.claim_on,
                                  lines=vlines, created_by=actor,
                                  memo=f"경비 청구 승인 {c.title} (#{c.id})")
        c.status, c.voucher_ref = "APPROVED", f"expense:{c.id}"
        return c if v else c

    async def pay(self, session: AsyncSession, *, claim_id: int, paid_on: date,
                  actor: int) -> dict:
        """지급 — 개인분(2110)만 DR 2110/CR 1110. 카드분은 월정산에서. 전액 카드면 전표 없이 PAID."""
        c = await self._get(session, claim_id)
        if c.status != "APPROVED":
            raise ValidationError("지급 가능한 상태가 아닙니다", {"status": c.status})
        personal = sum(float(ln.amount) for ln in await self._lines(session, claim_id)
                       if ln.card_tx_id is None)
        if personal > 0:
            await self._vs.create(session, source_module="expense",
                                  source_ref=f"expense-pay:{c.id}", voucher_on=paid_on,
                                  lines=[VoucherLineDTO(ACC_PAYABLE, "DEBIT", personal),
                                         VoucherLineDTO(ACC_CASH, "CREDIT", personal)],
                                  created_by=actor, memo=f"경비 지급 {c.title} (#{c.id})")
            c.payment_voucher_ref = f"expense-pay:{c.id}"
        c.status, c.paid_on = "PAID", paid_on
        return {"claim_id": c.id, "paid": personal, "card_deferred":
                float(c.total_amount) - personal, "paid_on": paid_on.isoformat()}

    # ── 법인카드 ────────────────────────────────────────────
    async def register_card(self, session: AsyncSession, *, alias: str, card_last4: str,
                            holder_employee_id: int | None = None,
                            department_id: int | None = None,
                            note: str | None = None) -> CorporateCard:
        if not (card_last4.isdigit() and len(card_last4) == 4):
            raise ValidationError("card_last4는 숫자 4자리입니다(전체 카드번호 저장 금지)")
        card = CorporateCard(alias=alias, card_last4=card_last4,
                             holder_employee_id=holder_employee_id,
                             department_id=department_id, note=note)
        session.add(card)
        await session.flush()
        return card

    async def import_transactions(self, session: AsyncSession, *, card_id: int,
                                  rows: list[dict]) -> dict:
        """사용내역 bulk 적재 — tx_id(카드사 승인번호) 멱등(중복 스킵)."""
        card = await session.get(CorporateCard, card_id)
        if card is None or not card.is_active:
            raise ValidationError("존재하지 않거나 비활성인 카드입니다", {"card_id": card_id})
        existing = set((await session.scalars(
            select(CardTransaction.tx_id).where(
                CardTransaction.tx_id.in_([str(r["tx_id"]) for r in rows])))).all())
        added = skipped = 0
        for r in rows:
            txid = str(r["tx_id"])
            if txid in existing:
                skipped += 1
                continue
            amt = round(float(r["amount"]))
            if amt <= 0:
                raise ValidationError("사용금액은 0보다 커야 합니다", {"tx_id": txid})
            session.add(CardTransaction(card_id=card_id, tx_id=txid, tx_on=r["tx_on"],
                                        merchant=r.get("merchant"), amount=amt))
            existing.add(txid)
            added += 1
        await session.flush()
        return {"card_id": card_id, "added": added, "skipped": skipped}

    async def settle_card_month(self, session: AsyncSession, *, card_id: int, yyyymm: str,
                                paid_on: date, actor: int) -> dict:
        """카드사 월정산 — 해당 월 CLAIMED(승인된 청구 귀속) 내역 합계를 DR 2112/CR 1110·SETTLED.

        대상 = tx_on이 해당 월·CLAIMED·연결 청구 status APPROVED/PAID(미승인 청구분은 부채 미인식
        상태라 제외 — 승인 후 재정산). 멱등: card-settle:{card}:{yyyymm} 전표(재실행 시 잔여분만).
        """
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])
        start, nxt = date(y, m, 1), date(y + (m == 12), (m % 12) + 1, 1)
        txs = (await session.scalars(
            select(CardTransaction).where(
                CardTransaction.card_id == card_id,
                CardTransaction.status == "CLAIMED",
                CardTransaction.tx_on >= start, CardTransaction.tx_on < nxt))).all()
        # 승인/지급된 청구에 귀속된 내역만 — 청구 id 일괄 조회(N+1 제거·동일청구 중복조회 방지)
        claim_ids = {t.claim_id for t in txs if t.claim_id}
        ok_claims = set()
        if claim_ids:
            ok_claims = set((await session.scalars(
                select(ExpenseClaim.id).where(
                    ExpenseClaim.id.in_(claim_ids),
                    ExpenseClaim.status.in_(("APPROVED", "PAID"))))).all())
        eligible = [t for t in txs if t.claim_id in ok_claims]
        total = round(sum(float(t.amount) for t in eligible))
        if total <= 0:
            return {"card_id": card_id, "yyyymm": yyyymm, "settled": 0, "count": 0}
        seq = int(await session.scalar(
            select(func.count()).select_from(CardTransaction).where(
                CardTransaction.card_id == card_id, CardTransaction.status == "SETTLED",
                CardTransaction.tx_on >= start, CardTransaction.tx_on < nxt)) or 0)
        ref = (f"card-settle:{card_id}:{yyyymm}" if seq == 0
               else f"card-settle:{card_id}:{yyyymm}:v{seq}")   # 잔여분 추가 정산도 안전
        await self._vs.create(session, source_module="expense", source_ref=ref,
                              voucher_on=paid_on,
                              lines=[VoucherLineDTO(ACC_CARD_PAYABLE, "DEBIT", total),
                                     VoucherLineDTO(ACC_CASH, "CREDIT", total)],
                              created_by=actor,
                              memo=f"법인카드 월정산 card#{card_id} {yyyymm}")
        for tx in eligible:
            tx.status, tx.settled_on = "SETTLED", paid_on
        return {"card_id": card_id, "yyyymm": yyyymm, "settled": total,
                "count": len(eligible), "voucher_ref": ref}
