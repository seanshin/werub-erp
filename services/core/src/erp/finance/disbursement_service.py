"""AP 지급결의 배치 (펌뱅킹 이체·P3-4) — 미지급금 다건 일괄 지급.

결의(DRAFT)→승인(APPROVED·SoD 작성자≠승인자)→실행(EXECUTED·라인별 SettlementService.pay 재전기
DR2110/CR1110)/취소(CANCELED). 미지급금은 활성 배치(비취소) 1개에만 포함(중복지급 방지). 이체파일은
표준 CSV(거래처·금액·예정일·계좌·미지급id)로 은행 무관 export.
"""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.doc_no import lock_doc_no
from erp.errors import ValidationError
from erp.finance.models import DisbursementBatch, DisbursementLine, Payable
from erp.finance.settlement_service import SettlementService


class DisbursementService:
    def __init__(self) -> None:
        self._settle = SettlementService()

    async def _next_no(self, session: AsyncSession, on: date) -> str:
        ym = on.strftime("%Y%m")
        await lock_doc_no(session, f"DISB-{ym}")
        n = int(await session.scalar(select(func.count()).select_from(DisbursementBatch)
                                     .where(DisbursementBatch.batch_no.like(f"DISB-{ym}-%"))) or 0) + 1
        return f"DISB-{ym}-{n:03d}"

    async def create_batch(self, session: AsyncSession, *, payable_ids: list[int],
                           scheduled_on: date, note: str | None = None,
                           accounts: dict[int, str] | None = None,
                           actor: int | None = None) -> DisbursementBatch:
        """미지급금 다건을 배치로 결의 — 각 미지급금 잔액 전액을 라인으로. 활성 배치 중복 포함 차단."""
        if not payable_ids:
            raise ValidationError("지급 대상 미지급금을 선택하세요")
        ids = list(dict.fromkeys(payable_ids))          # 중복 제거·순서 유지
        # 2026-10-09 전체 흐름 검토 E: 중복 포함 검사가 행 잠금보다 먼저라, 동시 결의 두 건이 둘 다
        # '활성 배치 없음'을 보고 같은 미지급금을 각자 담을 수 있었다(이중 지급 경로). 미지급금 행을
        # id 오름차순(교착 방지)으로 먼저 잠근 뒤 검사한다 — 두 번째 트랜잭션은 첫 커밋 후 검사.
        locked: dict[int, Payable] = {}
        for pid in sorted(ids):
            p = await session.get(Payable, pid, with_for_update=True)
            if p is None:
                raise ValidationError(f"존재하지 않는 미지급금: {pid}")
            locked[pid] = p
        # 이미 활성(비취소) 배치에 포함된 미지급금 차단
        active = set((await session.scalars(
            select(DisbursementLine.payable_id)
            .join(DisbursementBatch, DisbursementBatch.id == DisbursementLine.batch_id)
            .where(DisbursementLine.payable_id.in_(ids),
                   DisbursementBatch.status != "CANCELED"))).all())
        if active:
            raise ValidationError(f"이미 지급결의에 포함된 미지급금: {sorted(active)}")
        batch = DisbursementBatch(batch_no=await self._next_no(session, scheduled_on),
                                  status="DRAFT", scheduled_on=scheduled_on, note=note,
                                  created_by=actor)
        session.add(batch)
        await session.flush()
        total = 0.0
        acc = accounts or {}
        for pid in ids:
            p = locked[pid]
            outstanding = float(p.amount) - float(p.paid_amount)
            if p.status == "PAID" or outstanding <= 0:
                raise ValidationError(f"이미 지급 완료된 미지급금: {pid}")
            session.add(DisbursementLine(batch_id=batch.id, payable_id=pid, party=p.party,
                                         party_ref=p.party_ref, amount=outstanding, due_on=p.due_on,
                                         bank_account=acc.get(pid), status="PENDING"))
            total += outstanding
        batch.total_amount, batch.line_count = total, len(ids)
        await session.flush()
        return batch

    async def assert_not_in_open_batch(self, session: AsyncSession, *, payable_id: int) -> None:
        """미지급금이 결의 중(DRAFT/APPROVED) 배치에 있으면 개별 지급 차단.

        2026-10-09 전체 흐름 검토 E: 배치에 담긴 미지급금을 /payables/{id}/pay로 직접 지급하면
        배치 승인(SoD)을 건너뛰고, 이후 배치 실행이 같은 금액을 다시 지급하려다 실패/이중 이체된다.
        """
        bno = await session.scalar(
            select(DisbursementBatch.batch_no)
            .join(DisbursementLine, DisbursementBatch.id == DisbursementLine.batch_id)
            .where(DisbursementLine.payable_id == payable_id,
                   DisbursementBatch.status.in_(("DRAFT", "APPROVED"))))
        if bno is not None:
            raise ValidationError(
                f"지급결의 {bno}에 포함된 미지급금은 개별 지급할 수 없습니다 — "
                "자금 담당자가 그 결의를 실행하거나 취소한 뒤 지급하세요",
                {"payable_id": payable_id, "batch_no": bno})

    async def approve(self, session: AsyncSession, *, batch_id: int, actor: int) -> DisbursementBatch:
        b = await self._get(session, batch_id)
        if b.status != "DRAFT":
            raise ValidationError(f"승인 가능한 상태가 아닙니다 ({b.status})")
        if b.created_by is not None and actor == b.created_by:
            raise ValidationError("직무분리 — 작성자는 본인 결의를 승인할 수 없습니다")
        b.status, b.approved_by = "APPROVED", actor
        await session.flush()
        return b

    async def execute(self, session: AsyncSession, *, batch_id: int, on: date,
                      actor: int | None = None) -> dict:
        """실행 — 승인된 배치의 각 라인을 지급 전기(DR 미지급/CR 현금). 멱등(pay source_ref)."""
        b = await self._get(session, batch_id)
        if b.status != "APPROVED":
            raise ValidationError(f"실행 가능한 상태가 아닙니다 ({b.status})")
        lines = (await session.scalars(select(DisbursementLine).where(
            DisbursementLine.batch_id == batch_id))).all()
        paid = 0.0
        for ln in lines:
            if ln.status == "PAID":
                continue
            await self._settle.pay(session, payable_id=ln.payable_id, amount=float(ln.amount),
                                   on=on, created_by=actor)
            ln.status = "PAID"
            paid += float(ln.amount)
        b.status, b.executed_on = "EXECUTED", on
        await session.flush()
        return {"id": b.id, "status": b.status, "paid_total": round(paid), "lines": len(lines)}

    async def cancel(self, session: AsyncSession, *, batch_id: int) -> DisbursementBatch:
        b = await self._get(session, batch_id)
        if b.status == "EXECUTED":
            raise ValidationError("이미 실행된 배치는 취소할 수 없습니다")
        b.status = "CANCELED"
        await session.flush()
        return b

    async def list_batches(self, session: AsyncSession, *, status: str | None = None) -> list[dict]:
        q = select(DisbursementBatch).order_by(DisbursementBatch.id.desc())
        if status:
            q = q.where(DisbursementBatch.status == status)
        rows = (await session.scalars(q.limit(300))).all()
        return [{"id": b.id, "batch_no": b.batch_no, "status": b.status,
                 "scheduled_on": str(b.scheduled_on), "total_amount": float(b.total_amount),
                 "line_count": b.line_count, "note": b.note,
                 "executed_on": str(b.executed_on) if b.executed_on else None} for b in rows]

    async def detail(self, session: AsyncSession, *, batch_id: int) -> dict:
        b = await self._get(session, batch_id)
        lines = (await session.scalars(select(DisbursementLine).where(
            DisbursementLine.batch_id == batch_id).order_by(DisbursementLine.id))).all()
        return {"id": b.id, "batch_no": b.batch_no, "status": b.status,
                "scheduled_on": str(b.scheduled_on), "total_amount": float(b.total_amount),
                "line_count": b.line_count, "note": b.note,
                "executed_on": str(b.executed_on) if b.executed_on else None,
                "lines": [{"id": ln.id, "payable_id": ln.payable_id, "party": ln.party,
                           "amount": float(ln.amount),
                           "due_on": str(ln.due_on) if ln.due_on else None,
                           "bank_account": ln.bank_account, "status": ln.status} for ln in lines]}

    async def export_csv(self, session: AsyncSession, *, batch_id: int) -> str:
        """이체파일(표준 CSV) — 은행 무관. 헤더: 거래처,금액,지급예정일,계좌,미지급ID."""
        d = await self.detail(session, batch_id=batch_id)
        # 2026-10-09 전체 흐름 검토 E: DRAFT(미승인) 배치도 이체파일이 나와 은행 업로드로 SoD 승인을
        # 우회할 수 있었다. 승인 완료(APPROVED) 배치만 — 실행 후 재출력은 이중 이체 위험이라 불가.
        if d["status"] != "APPROVED":
            raise ValidationError(
                f"승인된 지급결의만 이체파일을 만들 수 있습니다 ({d['status']}) — "
                "작성자 외 승인권자가 먼저 승인하세요", {"batch_id": batch_id})
        rows = ["거래처,금액,지급예정일,계좌,미지급ID"]
        for ln in d["lines"]:
            party = str(ln["party"]).replace(",", " ")
            acct = (ln["bank_account"] or "").replace(",", " ")
            rows.append(f"{party},{int(ln['amount'])},{d['scheduled_on']},{acct},{ln['payable_id']}")
        return "\n".join(rows) + "\n"

    async def _get(self, session: AsyncSession, batch_id: int) -> DisbursementBatch:
        b = await session.get(DisbursementBatch, batch_id)
        if b is None:
            raise ValidationError("존재하지 않는 지급결의입니다")
        return b
