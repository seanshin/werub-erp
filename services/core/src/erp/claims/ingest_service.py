"""통보서 스테이징 → 코어 반영 (FR-38/39) — worker-batch 잡의 본체.

파이프라인 (design §5.2): staging NEW/REPROCESSED → 파일 읽기 → MEDRLT 파싱
→ claim_batches/statements/adjustments 반영 → OK. 실패 행은 FAILED + error_detail (격리).
멱등: receipt_no UNIQUE — 기존 배치 존재 시 스킵 (중복 반영 불가).

⚠️ G1 운영 게이트: 한정자 잠정값 — 실물 2건 검증 통과 전 운영 데이터 반영 금지
   (settings.medrlt_live_enabled 플래그로 차단, 기본 False).
"""
import logging
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.medrlt_parser import MedrltParseError, parse_medrlt_file
from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement, NoticeFileStaging
from erp.errors import ValidationError

logger = logging.getLogger("erp.claims.ingest")

ENCODINGS = ("utf-8", "cp949", "euc-kr")


def _decode(raw: bytes) -> str:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise MedrltParseError("지원 인코딩(utf-8/cp949)으로 해독 불가")


def _to_date(yyyymmdd: str | None) -> date | None:
    if not yyyymmdd or len(yyyymmdd) < 8:
        return None
    try:
        return datetime.strptime(yyyymmdd[:8], "%Y%m%d").date()
    except ValueError:
        return None


class NoticeIngestService:
    def __init__(self, storage_dir: str = "staging-files"):
        self._dir = Path(storage_dir)

    async def process_pending(self, session: AsyncSession, *, limit: int = 20) -> dict:
        """NEW/REPROCESSED 스테이징 일괄 처리 — 결과 카운트 반환.

        ⚠️ G1 게이트: medrlt_live_enabled=False(기본)면 처리 차단 — staging은 NEW로
        보존되어 실물 검증 후 플래그를 켜면 다음 회차에 반영(잠정 한정자 무게이트 유입 방지).
        """
        from erp.config import get_settings
        if not get_settings().medrlt_live_enabled:
            logger.info("통보서 반영 게이트 차단 — medrlt_live_enabled=False (실물 검증 전 보류)")
            return {"ok": 0, "failed": 0, "skipped": 0, "gated": True}
        rows = (await session.execute(
            select(NoticeFileStaging)
            .where(NoticeFileStaging.status.in_(["NEW", "REPROCESSED"]))
            .order_by(NoticeFileStaging.id).limit(limit)
        )).scalars().all()
        ok = failed = skipped = 0
        for staging in rows:
            result = await self.process_one(session, staging)
            ok += result == "OK"
            failed += result == "FAILED"
            skipped += result == "SKIPPED"
        return {"ok": ok, "failed": failed, "skipped": skipped}

    async def process_one(self, session: AsyncSession,
                          staging: NoticeFileStaging) -> str:
        try:
            path = self._dir / staging.stored_name
            if not path.exists():
                raise MedrltParseError(f"스테이징 파일 없음: {staging.stored_name}")
            # 수신 파일은 복수 전문 누적 가능 (청구 SW 실무 사양) — 전문별 독립 반영
            notices = parse_medrlt_file(_decode(path.read_bytes()))
            created = skipped_count = 0
            for notice in notices:
                result = await self._persist_notice(session, staging, notice)
                created += result == "OK"
                skipped_count += result == "SKIPPED"
            staging.status = "OK"
            staging.error_detail = (
                {"messages": len(notices), "created": created, "skipped": skipped_count}
                if len(notices) > 1 or skipped_count else staging.error_detail)
            return "OK" if created else "SKIPPED"
        except MedrltParseError as exc:
            # 실패 행 격리 (design §5.2) — reprocess API로 재시도 가능
            staging.status = "FAILED"
            staging.error_detail = {"error": str(exc)}
            logger.warning("통보서 파싱 실패 — 격리",
                           extra={"ctx": {"staging": staging.id, "error": str(exc)}})
            return "FAILED"

    async def _persist_notice(self, session: AsyncSession,
                              staging: NoticeFileStaging, notice) -> str:
        # 멱등 — 동일 접수번호 재처리 시 스킵 (중복 반영 불가, design §5.2)
        existing = (await session.execute(
            select(ClaimBatch).where(ClaimBatch.receipt_no == notice.receipt_no)
        )).scalar_one_or_none()
        if existing is not None:
            return "SKIPPED"

        batch = ClaimBatch(
            receipt_no=notice.receipt_no,
            # 진료월 키 — notified_on이 8자리 미만이면 "0000-00"(짧은 값에 "2026-" 같은 불완전 키 생성 방지)
            care_month=(f"{notice.notified_on[:4]}-{notice.notified_on[4:6]}"
                        if notice.notified_on and len(notice.notified_on) >= 6 else "0000-00"),
            decided_on=_to_date(notice.notified_on),
            # 기한 기산일 = 실제 파일 수신일(staging.received_at) — 처리일(date.today())이 아님.
            # G1 게이트로 수신 후 수일~수주 뒤 파싱하는 정상 흐름에서, 처리일을 쓰면 기산일이 뒤로
            # 밀려 이의신청 90일 산정 기한이 실제(수신+90)보다 늦게 나와 도과 위험(FR-40).
            notice_arrived_on=(staging.received_at.date() if staging.received_at
                               else date.today()),
            claimed_amt=notice.claimed_amt,
            decided_amt=notice.decided_amt,
            staging_id=staging.id)
        session.add(batch)
        await session.flush()

        for idx, ps in enumerate(notice.statements):
            stmt = ClaimStatement(
                batch_id=batch.id,
                # 일련번호 누락 시 인덱스 폴백 — 무번호 명세가 2건 이상이면 "?" 중복으로 uq_stmt 위반,
                # 이 IntegrityError는 process_one이 잡지 못해 배치잡 전체를 오염시킴(격리 실패).
                stmt_serial=ps.stmt_serial or f"#{idx + 1}",
                chart_no=ps.chart_ref, dept_code=ps.dept_code,
                claimed_amt=ps.claimed_amt, adjusted_amt=ps.adjusted_amt,
                decided_amt=ps.decided_amt)
            session.add(stmt)
            await session.flush()
            for pa in ps.adjustments:
                session.add(ClaimAdjustment(
                    statement_id=stmt.id, adj_code=pa.adj_code,
                    adj_amt=pa.adj_amt, adj_reason=pa.adj_reason,
                    # 한정자 매핑 성공 여부로 자동화율 분자 결정 (Plan §4.3)
                    map_status="AUTO" if pa.adj_code else "UNMAPPED"))

        if notice.unknown_segments:
            staging.error_detail = {"unknown_segments": notice.unknown_segments[:20]}
        await session.flush()
        # 배치 지급 생애주기 상태 갱신 — 이미 지급된 월에 지각 통보서가 들어오면 재계산(부분지급 복원)
        from erp.claims.service import ClaimPaymentService
        _pay = ClaimPaymentService()
        await _pay.refresh_status(session, batch.care_month)
        # 결정 도착 → 과다지급 선수금(2141) 상계(DR 2141/CR 1140) — 잔액=목표면 no-op(멱등)
        await _pay.reclass_advance(session, care_month=batch.care_month)
        logger.info("통보서 반영", extra={"ctx": {
            "staging": staging.id, "receipt_no": notice.receipt_no,
            "statements": len(notice.statements)}})
        return "OK"


async def ingest_medrlt_json(session, *, payload: dict, actor: int) -> dict:
    """MEDRLT JSON 적재 (합성 샘플 E2E·향후 JSON 채널) — receipt_no 멱등.

    실물 KEDI 텍스트 레이아웃은 기존 파서(medrlt_parser) 경로 — 본 함수는
    HIS 합성 샘플(핵심 필드 구조)·JSON 변환 수신용. TEST- 식별자는 통계에서 구분.
    """
    from sqlalchemy import select

    from erp.claims.models import ClaimAdjustment, ClaimBatch, ClaimStatement

    notice = payload.get("notice") or {}
    receipt_no = notice.get("noticeNo") or notice.get("claimNo")
    if not receipt_no:
        raise ValidationError("notice.noticeNo 필수")   # API 경로 — 전역 핸들러로 422
    existing = (await session.execute(select(ClaimBatch).where(
        ClaimBatch.receipt_no == receipt_no))).scalar_one_or_none()
    if existing is not None:
        return {"batch_id": existing.id, "duplicate": True}

    ym_raw = str(notice.get("reviewYm") or "")
    care_month = f"{ym_raw[:4]}-{ym_raw[4:6]}" if len(ym_raw) == 6 else ym_raw
    summary = payload.get("summary") or {}
    batch = ClaimBatch(
        receipt_no=receipt_no, care_month=care_month,
        decided_on=_to_date(str(notice.get("noticedAt") or "").replace("-", "")),
        notice_arrived_on=_to_date(str(notice.get("noticedAt") or "").replace("-", ""))
        or date.today(),
        claimed_amt=float(summary.get("totalClaimed") or 0) or None,
        decided_amt=(float(summary.get("totalClaimed") or 0)
                     - float(summary.get("totalReduced") or 0)) or None)
    session.add(batch)
    await session.flush()

    stmts = adjs = 0
    for st in payload.get("statements") or []:
        stmt = ClaimStatement(
            batch_id=batch.id, stmt_serial=str(st.get("statementSeq") or stmts + 1).zfill(4),
            chart_no=st.get("chartNo"),
            claimed_amt=float(st.get("claimedAmount") or 0),
            adjusted_amt=float(st.get("adjustedAmount") or 0),
            decided_amt=float(st.get("reviewedAmount") or 0))
        session.add(stmt)
        await session.flush()
        stmts += 1
        for adj in st.get("adjustments") or []:
            session.add(ClaimAdjustment(
                statement_id=stmt.id,
                adj_code=adj.get("adjustReasonCode") or adj.get("adjustCode"),
                adj_category=adj.get("adjustCode"),
                adj_amt=float(adj.get("reducedAmount") or 0),
                adj_reason=adj.get("adjustReason"),
                map_status="AUTO" if adj.get("adjustReasonCode") else "UNMAPPED"))
            adjs += 1
    await session.flush()
    from erp.claims.service import ClaimPaymentService
    _pay = ClaimPaymentService()
    await _pay.refresh_status(session, care_month)   # 배치 지급 생애주기 상태
    await _pay.reclass_advance(session, care_month=care_month, actor=actor)  # 선수금 상계(멱등)
    return {"batch_id": batch.id, "duplicate": False,
            "statements": stmts, "adjustments": adjs,
            "synthetic": payload.get("marker") == "TEST-SYNTHETIC"}
