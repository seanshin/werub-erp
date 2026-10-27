"""결재 불변 원장 서비스 — HMAC 서명 해시체인 + 앵커 체크포인트(변조 증거).

체인: row_mac = HMAC-SHA256(key, prev_mac|seq|doc_type|doc_id|instance|step|action|actor|acted_at|note).
- **키 기반 서명(v1.143)**: 무키 SHA-256은 쓰기권한자(superuser)가 전 체인을 재계산·재작성해 무탐지
  위조 가능(적대검토 HIGH-1). HMAC 키는 앱만 보유 → DB 직접쓰기로는 유효 MAC 생성 불가 → verify 탐지.
- **앵커(v1.143)**: verify가 head를 앵커에 봉인 → 최근 행 삭제(꼬리 절단·HIGH-2) 탐지(해시체인 사각).
append는 PG advisory lock 직렬화(seq·prev 경합 방지). 테이블은 DB 계층 append-only(트리거·0204) →
verify가 전 구간 재계산 + 앵커 대조로 변조·삭제·꼬리절단을 사후 탐지.
"""
import hashlib
import hmac
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import ApprovalLedger, ApprovalLedgerAnchor
from erp.config import get_settings
from erp.common.kst import kst_iso

GENESIS_HASH = "0" * 64
_CHAIN_LOCK_KEY = 0x6170764C           # 'apvL' — 전역 체인 advisory lock


def _signing_key() -> bytes:
    """결재 원장 MAC 서명 키 — 전용 키(APPROVAL_LEDGER_KEY) 우선, 미설정 시 jwt_secret 도메인 분리 파생.

    파생도 무키보다 강함(키가 앱 밖으로 안 나감). prod는 jwt_secret이 파일주입 필수(fail-closed).
    """
    s = get_settings()
    if s.approval_ledger_key:
        return s.approval_ledger_key.encode()
    return hmac.new(s.jwt_secret.encode(), b"approval-ledger-mac-v1", hashlib.sha256).digest()


def _row_mac(key: bytes, prev_hash: str, *, seq_no: int, doc_type: str, doc_id: int,
             instance_id: int, step_no: int | None, action: str, actor: int,
             acted_at: datetime, note: str | None) -> str:
    """결정 필드 HMAC — acted_at은 UTC naive 정규화 후 초단위 ISO(PG aware↔sqlite naive 표현차 방지)."""
    norm = (acted_at.astimezone(timezone.utc).replace(tzinfo=None)
            if acted_at.tzinfo is not None else acted_at)
    at = norm.replace(microsecond=0).isoformat()
    payload = (f"{prev_hash}|{seq_no}|{doc_type}|{doc_id}|{instance_id}"
               f"|{step_no if step_no is not None else ''}|{action}|{actor}|{at}|{note or ''}")
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def _anchor_mac(key: bytes, *, seq_no: int, entry_count: int, head_hash: str) -> str:
    return hmac.new(key, f"{seq_no}|{entry_count}|{head_hash}".encode(),
                    hashlib.sha256).hexdigest()


class ApprovalLedgerService:
    async def _lock(self, session: AsyncSession) -> None:
        """전역 체인 직렬화 — sqlite(테스트)는 no-op."""
        if session.bind.dialect.name == "postgresql":
            await session.execute(select(func.pg_advisory_xact_lock(_CHAIN_LOCK_KEY)))

    async def _last(self, session: AsyncSession) -> ApprovalLedger | None:
        return await session.scalar(
            select(ApprovalLedger).order_by(ApprovalLedger.seq_no.desc()).limit(1))

    async def append(self, session: AsyncSession, *, instance_id: int, doc_type: str,
                     doc_id: int, step_no: int | None, action: str, actor: int,
                     acted_at: datetime, note: str | None = None) -> ApprovalLedger:
        await self._lock(session)
        key = _signing_key()
        last = await self._last(session)
        seq = (last.seq_no + 1) if last else 1
        prev = last.row_hash if last else GENESIS_HASH
        note = (note or None) and note[:300]
        row = ApprovalLedger(
            seq_no=seq, instance_id=instance_id, doc_type=doc_type, doc_id=doc_id,
            step_no=step_no, action=action, actor=actor, acted_at=acted_at,
            note=note, prev_hash=prev,
            row_hash=_row_mac(key, prev, seq_no=seq, doc_type=doc_type, doc_id=doc_id,
                              instance_id=instance_id, step_no=step_no, action=action,
                              actor=actor, acted_at=acted_at, note=note))
        session.add(row)
        await session.flush()
        return row

    async def verify(self, session: AsyncSession) -> dict:
        """전 구간 MAC 재계산 + 앵커 대조 — 변조·삭제·순서교란·꼬리절단을 최초 파손 지점으로 보고."""
        key = _signing_key()
        rows = (await session.scalars(
            select(ApprovalLedger).order_by(ApprovalLedger.seq_no))).all()
        prev = GENESIS_HASH
        hash_at_seq: dict[int, str] = {}
        for i, r in enumerate(rows):
            expected_seq = i + 1
            recomputed = _row_mac(key, prev, seq_no=r.seq_no, doc_type=r.doc_type,
                                  doc_id=r.doc_id, instance_id=r.instance_id,
                                  step_no=r.step_no, action=r.action, actor=r.actor,
                                  acted_at=r.acted_at, note=r.note)
            if r.seq_no != expected_seq or r.prev_hash != prev or r.row_hash != recomputed:
                return {"ok": False, "entries": len(rows), "first_broken_seq": r.seq_no,
                        "reason": ("순서 교란/행 삭제" if r.seq_no != expected_seq
                                   else "체인 링크 불일치" if r.prev_hash != prev
                                   else "행 내용 변조(서명 불일치)")}
            prev = r.row_hash
            hash_at_seq[r.seq_no] = prev

        # 앵커 대조 — 최근 행 삭제(꼬리 절단)는 남은 체인이 유효해 위 루프로 못 잡음
        anchor = await session.scalar(
            select(ApprovalLedgerAnchor).order_by(ApprovalLedgerAnchor.seq_no.desc()).limit(1))
        if anchor is not None:
            if anchor.mac != _anchor_mac(key, seq_no=anchor.seq_no,
                                         entry_count=anchor.entry_count, head_hash=anchor.head_hash):
                return {"ok": False, "entries": len(rows), "first_broken_seq": anchor.seq_no,
                        "reason": "앵커 변조"}
            if len(rows) < anchor.entry_count or anchor.seq_no not in hash_at_seq:
                return {"ok": False, "entries": len(rows), "first_broken_seq": anchor.seq_no,
                        "reason": "꼬리 절단(앵커 이후 행 소실)"}
            if hash_at_seq[anchor.seq_no] != anchor.head_hash:
                return {"ok": False, "entries": len(rows), "first_broken_seq": anchor.seq_no,
                        "reason": "앵커 지점 해시 불일치"}
        return {"ok": True, "entries": len(rows), "head_hash": prev,
                "anchored_seq": anchor.seq_no if anchor else 0}

    async def checkpoint(self, session: AsyncSession) -> dict:
        """검증 통과한 현재 head를 앵커로 봉인 — 이후 꼬리 절단 탐지 기준점. 신규 행 없으면 미생성.

        **append와 동일한 advisory lock으로 직렬화**한다(적대검토 C-H3). 락 없이 verify→_last를
        따로 읽으면 그 사이 커밋된 append 때문에 `seq_no=N+1`인데 `head_hash=hash_at_N`인
        **자기모순 앵커**가 박힌다. 그 앵커는 0204 트리거로 UPDATE/DELETE 불가라 이후 verify가
        영구 "앵커 지점 해시 불일치"로 실패하고(매일 허위 변조 경보 + 신규 앵커 생성 불가),
        v1.259.0부터는 그 모순값이 sign append-only 체인에 **TSA로 영구 각인**돼 취소도 안 된다.
        """
        await self._lock(session)
        chk = await self.verify(session)
        if not chk["ok"]:
            return {"created": False, "reason": chk["reason"], "verify": chk}
        if chk["entries"] == 0:
            return {"created": False, "reason": "빈 체인"}
        last = await self._last(session)
        prev_anchor = await session.scalar(
            select(ApprovalLedgerAnchor).order_by(ApprovalLedgerAnchor.seq_no.desc()).limit(1))
        if prev_anchor is not None and prev_anchor.seq_no == last.seq_no:
            return {"created": False, "reason": "신규 엔트리 없음", "seq_no": last.seq_no}
        key = _signing_key()
        anchor = ApprovalLedgerAnchor(
            seq_no=last.seq_no, entry_count=chk["entries"], head_hash=chk["head_hash"],
            mac=_anchor_mac(key, seq_no=last.seq_no, entry_count=chk["entries"],
                            head_hash=chk["head_hash"]))
        session.add(anchor)
        await session.flush()
        # head_hash를 함께 반환 — 호출자가 별도 verify 결과를 쓰면 앵커와 다른 값을 외부에
        # 봉인할 수 있다(같은 경쟁의 다른 얼굴). 봉인 대상은 반드시 이 앵커 행의 값이어야 한다.
        return {"created": True, "seq_no": anchor.seq_no, "entry_count": anchor.entry_count,
                "head_hash": anchor.head_hash}

    async def hash_at(self, session: AsyncSession, seq_no: int) -> str | None:
        """seq_no 지점까지의 체인 head 해시를 재계산 — 외부 봉인값과 대조하기 위한 기준값.

        verify()와 동일한 재계산이되 지정 지점에서 멈춘다. 봉인 당시의 원장 상태를 지금 원장으로
        복원해 보는 것이므로, 값이 다르면 **그 지점까지의 과거가 재작성됐다**는 뜻이다.
        """
        key = _signing_key()
        rows = (await session.scalars(
            select(ApprovalLedger).where(ApprovalLedger.seq_no <= seq_no)
            .order_by(ApprovalLedger.seq_no))).all()
        if not rows or rows[-1].seq_no != seq_no:
            return None                      # 봉인된 지점까지 존재하지 않음(꼬리 절단·재작성)
        prev = GENESIS_HASH
        for i, r in enumerate(rows):
            if r.seq_no != i + 1:
                return None
            prev = _row_mac(key, prev, seq_no=r.seq_no, doc_type=r.doc_type, doc_id=r.doc_id,
                            instance_id=r.instance_id, step_no=r.step_no, action=r.action,
                            actor=r.actor, acted_at=r.acted_at, note=r.note)
        return prev

    async def entries_for(self, session: AsyncSession, *, doc_type: str,
                          doc_id: int) -> list[dict]:
        """문서별 결재 원장 로그(시간순) — 결재 이력의 불변 증거 노출."""
        rows = (await session.scalars(
            select(ApprovalLedger).where(ApprovalLedger.doc_type == doc_type,
                                         ApprovalLedger.doc_id == doc_id)
            .order_by(ApprovalLedger.seq_no))).all()
        return [{"seq_no": r.seq_no, "action": r.action, "step_no": r.step_no,
                 "actor": r.actor, "note": r.note,
                 "acted_at": kst_iso(r.acted_at),
                 "row_hash": r.row_hash, "prev_hash": r.prev_hash} for r in rows]
