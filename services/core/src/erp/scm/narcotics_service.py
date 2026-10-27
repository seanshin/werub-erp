"""마약류 수불부 (FR-20/21) — design §3.4 동시성·무결성 규칙.

① 채번 직렬화: PG는 pg_advisory_xact_lock(품목 단위) — sqlite 테스트는 순차 실행 전제
② balance_after 시스템 산출 (사용자 입력 금지)
③ CORRECTION은 원본(IN/OUT/DISPOSE/ADJUST)만 참조 — 정정의 정정도 원본 참조, 최신만 유효
④ 해시 체인: row_hash = SHA-256(prev_hash ‖ 필드) — 주기 검증 잡이 재계산 대조
"""
import hashlib
from datetime import date, datetime, timezone

from sqlalchemy import Date, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.scm.models import Item, NarcoticsLedger

SIGN = {"IN": 1, "OUT": -1, "DISPOSE": -1, "ADJUST": 1, "CORRECTION": 1}
GENESIS_HASH = "0" * 64
_NARC_DEDUP_WINDOW_S = 15   # 직전과 동일한 수불 재기입 차단 창(더블클릭·재시도)


def _row_hash(prev_hash: str, *, item_id: int, seq_no: int, entry_type: str,
              qty: float, balance_after: float, handler_id: int) -> str:
    # 수치는 소수 2자리 고정 포맷 — int/float/Decimal 표현 차이로 인한 해시 불일치 방지
    payload = (f"{prev_hash}|{item_id}|{seq_no}|{entry_type}"
               f"|{float(qty):.2f}|{float(balance_after):.2f}|{handler_id}")
    return hashlib.sha256(payload.encode()).hexdigest()


class NarcoticsService:
    async def add_entry(self, session: AsyncSession, *, item_id: int, entry_type: str,
                        qty: float, handler_id: int, lot_id: int | None = None,
                        entry_on: date | None = None) -> NarcoticsLedger:
        """수불 기록 — append-only. qty는 절대값 입력, 부호는 entry_type이 결정.

        entry_on=거래일(월 수불일지 귀속, FR-21). 미지정 시 오늘.
        """
        if entry_type == "CORRECTION":
            raise ValidationError("정정은 correct() 경로만 허용됩니다")
        if qty <= 0:
            raise ValidationError("수량은 0보다 커야 합니다")
        item = (await session.execute(
            select(Item).where(Item.id == item_id))).scalar_one_or_none()
        if item is None or not item.is_narcotic:
            raise ValidationError("마약류 품목이 아닙니다")

        await self._acquire_item_lock(session, item_id)  # ① 직렬화
        last = await self._last_entry(session, item_id)
        eff_on = entry_on or date.today()
        # 시계열 무결성 — balance_after/seq는 삽입순(tail) 산출이라, 직전 기록보다 이른 거래일을
        # append하면 그 과거월 수불일지의 잔량 컬럼이 글로벌 누적으로 튀어 비단조가 됨(마약류 수불부 결함).
        if last and last.entry_on and eff_on < last.entry_on:
            raise ValidationError(
                "거래일은 직전 기록일 이후여야 합니다 (마약류 수불부 시계열 무결성)",
                {"entry_on": eff_on.isoformat(), "last_entry_on": last.entry_on.isoformat()})
        prev_balance = float(last.balance_after) if last else 0.0
        prev_hash = last.row_hash if last else GENESIS_HASH
        seq_no = (last.seq_no + 1) if last else 1

        delta = qty * SIGN[entry_type]
        # 서버 멱등 — 직전 기록이 동일(품목·구분·수량·로트·취급자·거래일)하고 방금(창 이내) 생성됐으면
        # 중복 기입(더블클릭/재시도)으로 차단(마약류 원장 오염 방지). 로트까지 대조해 다른 로트 동일수량
        # 입고는 정상 통과. 아이템 락으로 직렬화돼 tail(last)만 봐도 충분.
        if (last is not None and last.entry_type == entry_type and float(last.qty) == delta
                and last.handler_id == handler_id and last.entry_on == eff_on
                and last.lot_id == lot_id and last.created_at is not None):
            # created_at은 PG=aware·sqlite=naive → UTC로 정규화 후 경과시간 비교
            ca = last.created_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - ca).total_seconds() < _NARC_DEDUP_WINDOW_S:
                raise ValidationError(
                    "직전과 동일한 수불이 방금 기록됐습니다 (중복 기입 방지 — 잠시 후 재시도)",
                    {"item_id": item_id, "last_seq_no": last.seq_no})
        balance = prev_balance + delta
        if balance < 0:
            raise ValidationError("잔량이 음수가 됩니다", {"balance": balance})

        entry = NarcoticsLedger(
            seq_no=seq_no, item_id=item_id, lot_id=lot_id, entry_type=entry_type,
            qty=delta, balance_after=balance, handler_id=handler_id,
            entry_on=eff_on,
            row_hash=_row_hash(prev_hash, item_id=item_id, seq_no=seq_no,
                               entry_type=entry_type, qty=delta,
                               balance_after=balance, handler_id=handler_id))
        session.add(entry)
        await session.flush()
        return entry

    async def correct(self, session: AsyncSession, *, original_id: int, corrected_qty: float,
                      reason: str, handler_id: int,
                      entry_on: date | None = None) -> NarcoticsLedger:
        """③ 정정 — 원본 보존, 차액만 CORRECTION 행으로 append (balance 시스템 산출).

        entry_on=정정 거래일(미지정 시 오늘).
        """
        if not reason:
            raise ValidationError("정정 사유는 필수입니다")
        original = (await session.execute(
            select(NarcoticsLedger).where(NarcoticsLedger.id == original_id)
        )).scalar_one_or_none()
        if original is None:
            raise ValidationError("존재하지 않는 수불 기록입니다")
        if original.entry_type == "CORRECTION":
            raise ValidationError("정정 레코드는 직접 정정할 수 없습니다 — 원본을 다시 정정하세요")

        await self._acquire_item_lock(session, original.item_id)
        # 유효 수량 = 원본 + 기존 정정 누적 → 신규 정정은 (corrected - 유효) 차액
        prior_corrections = (await session.execute(
            select(NarcoticsLedger).where(NarcoticsLedger.corrects_id == original_id)
        )).scalars().all()
        effective = float(original.qty) + sum(float(c.qty) for c in prior_corrections)
        sign = 1 if float(original.qty) >= 0 else -1
        delta = (corrected_qty * sign) - effective
        if delta == 0:
            raise ValidationError("정정할 차액이 없습니다")

        last = await self._last_entry(session, original.item_id)
        eff_on = entry_on or date.today()
        if last and last.entry_on and eff_on < last.entry_on:   # 정정 거래일도 시계열 단조(수불부 무결성)
            raise ValidationError(
                "정정 거래일은 직전 기록일 이후여야 합니다 (마약류 수불부 시계열 무결성)",
                {"entry_on": eff_on.isoformat(), "last_entry_on": last.entry_on.isoformat()})
        balance = float(last.balance_after) + delta
        if balance < 0:
            raise ValidationError("정정 결과 잔량이 음수가 됩니다", {"balance": balance})
        seq_no = last.seq_no + 1
        entry = NarcoticsLedger(
            seq_no=seq_no, item_id=original.item_id, lot_id=original.lot_id,
            entry_type="CORRECTION", qty=delta, balance_after=balance,
            handler_id=handler_id, corrects_id=original_id, correction_reason=reason,
            entry_on=eff_on,
            row_hash=_row_hash(last.row_hash, item_id=original.item_id, seq_no=seq_no,
                               entry_type="CORRECTION", qty=delta,
                               balance_after=balance, handler_id=handler_id))
        session.add(entry)
        await session.flush()
        return entry

    async def verify_chain(self, session: AsyncSession, *, item_id: int) -> list[int]:
        """④ 무결성 검증 — seq 순 balance 재계산 + 해시 체인 대조. 불일치 id 반환."""
        rows = (await session.execute(
            select(NarcoticsLedger).where(NarcoticsLedger.item_id == item_id)
            .order_by(NarcoticsLedger.seq_no))).scalars().all()
        bad: list[int] = []
        prev_hash, balance = GENESIS_HASH, 0.0
        for row in rows:
            balance += float(row.qty)
            expected = _row_hash(prev_hash, item_id=item_id, seq_no=row.seq_no,
                                 entry_type=row.entry_type, qty=float(row.qty),
                                 balance_after=balance, handler_id=row.handler_id)
            if row.row_hash != expected or float(row.balance_after) != balance:
                bad.append(row.id)
            prev_hash = row.row_hash
        return bad

    async def monthly_ledger(self, session: AsyncSession, *, item_id: int,
                             yyyymm: str) -> list[NarcoticsLedger]:
        """수불일지 출력 입력 (FR-21) — Excel/PDF 렌더링은 export 레이어."""
        y, m = int(yyyymm[:4]), int(yyyymm[5:7])              # 거래일(entry_on) 기준 월 귀속
        nxt = date(y + (m == 12), (m % 12) + 1, 1)
        # entry_on 없는 legacy 행(append-only 백필 불가)은 created_at 날짜로 폴백
        eff = func.coalesce(NarcoticsLedger.entry_on, cast(NarcoticsLedger.created_at, Date))
        rows = (await session.execute(
            select(NarcoticsLedger).where(
                NarcoticsLedger.item_id == item_id,
                eff >= date(y, m, 1), eff < nxt)
            .order_by(NarcoticsLedger.seq_no))).scalars().all()
        return list(rows)

    # ── 내부 ────────────────────────────────────────────────
    async def _acquire_item_lock(self, session: AsyncSession, item_id: int) -> None:
        """PG advisory lock — sqlite(테스트)는 no-op."""
        if session.bind.dialect.name == "postgresql":
            await session.execute(
                select(func.pg_advisory_xact_lock(0x6E617263, item_id)))  # 'narc'

    async def _last_entry(self, session: AsyncSession, item_id: int) -> NarcoticsLedger | None:
        return (await session.execute(
            select(NarcoticsLedger).where(NarcoticsLedger.item_id == item_id)
            .order_by(NarcoticsLedger.seq_no.desc()).limit(1))).scalar_one_or_none()
