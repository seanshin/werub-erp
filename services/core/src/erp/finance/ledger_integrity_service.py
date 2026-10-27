"""원장 무결성 서비스 (G11) — voucher_lines 해시 체인 체크포인트 + 검증.

voucher_lines는 append-only(P1-a: REVOKE + vouchers 트리거)이므로 id 오름차순 프리픽스는 불변.
체크포인트는 직전 이후 신규 라인만 해시에 반영해 O(신규)로 증분 확장(hot-path 부담 없음·배치 실행).
검증은 각 세그먼트를 재계산해 저장된 content_hash와 대조 → 트리거를 우회한 superuser 변조까지 탐지.
"""
import hashlib

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.finance.models import LedgerIntegrityCheckpoint, VoucherLine

GENESIS_HASH = "0" * 64
# 체크포인트가 진행 중 전기 트랜잭션을 기다리는 최대 시간 — 넘으면 이번 주기는 건너뛴다(다음 주기 재시도).
_HORIZON_LOCK_TIMEOUT = "5s"


async def _acquire_horizon_lock(session: AsyncSession) -> tuple[bool, str | None]:
    """체크포인트 수평선 확보 — 이 시점 이후 id ≤ max(가시 id)인 미커밋 라인이 없음을 보장.

    2026-10-09 전체 흐름 검토 D·HIGH-3: id는 INSERT 시점에 시퀀스로 받지만 **커밋 순서는 id 순서가
    아니다**. 긴 트랜잭션이 작은 id를 받아 둔 채 늦게 커밋하면, 그 사이 체크포인트는 `id > prev_id`로
    더 큰 id까지 봉인해 버리고 ① 다음 검증에서 그 구간에 없던 라인이 나타나 **거짓 변조 경보**,
    ② 다음 체크포인트는 `id > up_to_line_id`부터라 그 라인은 **영구히 체인 밖**에 남는다.

    해법: `LOCK TABLE voucher_lines IN SHARE MODE` — INSERT(ROW EXCLUSIVE)와 충돌하므로 이미 행을 넣은
    진행 중 트랜잭션이 모두 끝날 때까지 기다리고, 잠금을 쥔 동안엔 새 INSERT가 대기한다(시퀀스 nextval은
    INSERT가 테이블 잠금을 얻은 뒤 평가되므로 잠금 해제 후 들어오는 라인은 반드시 더 큰 id). READ
    COMMITTED에선 잠금 뒤 다음 SELECT가 새 스냅샷을 잡아 기다린 라인까지 본다 → 간극 없는 프리픽스.
    전기 경로마다 advisory lock을 심는 방식은 새 전기 경로가 생기면 조용히 깨지므로 테이블 잠금을 택했다.
    잠금 대기는 lock_timeout으로 묶는다(대기 중엔 새 전기도 줄을 서므로). 시간 초과면 세이브포인트만
    되돌리고 False — 호출측은 이번 주기를 건너뛴다(누락·거짓경보보다 지연이 낫다).
    sqlite(단위 테스트)는 단일 작성자라 동시 커밋 간극이 없어 잠금이 불필요하다."""
    if session.get_bind().dialect.name != "postgresql":
        return True, None
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    try:
        async with session.begin_nested():
            await session.execute(text(f"SET LOCAL lock_timeout = '{_HORIZON_LOCK_TIMEOUT}'"))
            # SHARE ROW EXCLUSIVE — INSERT를 막는 것은 SHARE와 같고, **자기 자신과도 충돌**해 수동 EP와
            # 배치 체크포인트가 동시에 같은 prev_hash로 체인을 가르던 경합도 막는다(3차 검토 D).
            await session.execute(text("LOCK TABLE finance.voucher_lines IN SHARE ROW EXCLUSIVE MODE"))
    except DBAPIError as exc:   # 세이브포인트 롤백 — SET LOCAL도 함께 되돌려진다
        # 판정은 SQLSTATE로 — 120자로 자른 문자열에선 asyncpg 접두부에 밀려 "lock timeout"이 사라져
        # 시간 초과를 권한 오류로 오경보했다(2차 검토 D). 절단은 사유 문구에만.
        orig = getattr(exc, "orig", None)
        code = getattr(orig, "sqlstate", None) or getattr(getattr(orig, "__cause__", None), "sqlstate", None)
        if code == "55P03" or "lock timeout" in str(exc).lower():
            return False, "timeout"
        msg = str(exc)[:120]
        # 권한(소유자 ACL에서 TRUNCATE까지 회수 등)·기타 오류 — 조용히 건너뛰면 체크포인트가 영영
        # 안 쌓인다. 사유를 돌려 호출측(배치)이 경보하게 한다.
        return False, f"error: {msg}"
    # 세이브포인트 커밋 후에도 SET LOCAL은 트랜잭션 끝까지 남으므로 이후 문장을 위해 원복
    await session.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return True, None


def _segment_hash(prev_hash: str, rows: list) -> str:
    """SHA-256(prev_hash ‖ 세그먼트 라인). 금액은 정수 고정 표현(원단위 Numeric(*,0))."""
    h = hashlib.sha256()
    h.update(prev_hash.encode())
    for lid, vid, acct, side, amt in rows:
        h.update(f"{lid}|{vid}|{acct}|{side}|{int(amt)}\n".encode())
    return h.hexdigest()


class LedgerIntegrityService:
    async def _last(self, session: AsyncSession) -> LedgerIntegrityCheckpoint | None:
        return await session.scalar(
            select(LedgerIntegrityCheckpoint).order_by(LedgerIntegrityCheckpoint.id.desc()).limit(1))

    async def checkpoint(self, session: AsyncSession, *, actor: int | None = None) -> dict:
        """직전 체크포인트 이후 신규 라인을 해시 체인에 append. 신규 없으면 미생성.

        수평선 잠금(_acquire_horizon_lock)을 먼저 잡아 늦게 커밋되는 작은 id 라인이 봉인 구간에서
        빠지지 않게 한다. 잠금을 못 얻으면 이번 주기는 건너뛴다(created=False·skipped=True)."""
        locked, why = await _acquire_horizon_lock(session)
        if not locked:
            if why == "timeout":
                return {"created": False, "skipped": True,
                        "reason": "진행 중 전기 트랜잭션 대기 시간 초과 — 다음 주기에 재시도"}
            return {"created": False, "skipped": True, "lock_error": True,
                    "reason": f"원장 수평선 잠금 실패({why}) — DB 권한/설정 점검 필요(관리자)"}
        last = await self._last(session)
        prev_id = last.up_to_line_id if last else 0
        prev_hash = last.content_hash if last else GENESIS_HASH
        rows = (await session.execute(
            select(VoucherLine.id, VoucherLine.voucher_id, VoucherLine.account_code,
                   VoucherLine.side, VoucherLine.amount)
            .where(VoucherLine.id > prev_id).order_by(VoucherLine.id))).all()
        if not rows:
            return {"created": False, "up_to_line_id": prev_id, "reason": "신규 라인 없음"}
        content_hash = _segment_hash(prev_hash, rows)
        seg_d = sum(int(r[4]) for r in rows if r[3] == "DEBIT")
        seg_c = sum(int(r[4]) for r in rows if r[3] == "CREDIT")
        cp = LedgerIntegrityCheckpoint(
            up_to_line_id=rows[-1][0],
            line_count=(last.line_count if last else 0) + len(rows),
            sum_debit=(int(last.sum_debit) if last else 0) + seg_d,
            sum_credit=(int(last.sum_credit) if last else 0) + seg_c,
            content_hash=content_hash, prev_hash=prev_hash, created_by=actor)
        session.add(cp)
        await session.flush()
        return {"created": True, "checkpoint_id": cp.id, "up_to_line_id": cp.up_to_line_id,
                "line_count": cp.line_count, "content_hash": content_hash}

    async def verify(self, session: AsyncSession) -> dict:
        """전체 체인 재계산 대조 — 최초 불일치(변조 의심) 지점 반환. ok=True면 무결."""
        cps = list((await session.execute(
            select(LedgerIntegrityCheckpoint).order_by(LedgerIntegrityCheckpoint.id))).scalars())
        prev_id, prev_hash = 0, GENESIS_HASH
        for cp in cps:
            rows = (await session.execute(
                select(VoucherLine.id, VoucherLine.voucher_id, VoucherLine.account_code,
                       VoucherLine.side, VoucherLine.amount)
                .where(VoucherLine.id > prev_id, VoucherLine.id <= cp.up_to_line_id)
                .order_by(VoucherLine.id))).all()
            recomputed = _segment_hash(prev_hash, rows)
            if recomputed != cp.content_hash or cp.prev_hash != prev_hash:
                return {"ok": False, "broken_checkpoint_id": cp.id,
                        "up_to_line_id": cp.up_to_line_id, "checkpoints": len(cps)}
            prev_id, prev_hash = cp.up_to_line_id, cp.content_hash
        return {"ok": True, "checkpoints": len(cps), "up_to_line_id": prev_id}
