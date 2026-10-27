"""Regulatory 유스케이스 — temporal 단가 조회(FR-35)·공시 관리(FR-51)·AI 발신 큐(FR-52/53).

UoW 규약(design §4.4): 쓰기 메서드는 session을 첫 인자로 받고 commit하지 않는다.
"""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import FeeNotFoundError, ValidationError
from erp.regulatory import content_hash as ch
from erp.regulatory import filter_gate
from erp.regulatory.models import AiDispatchQueue, FeeMaster, Notice, NoticeRevision

MAX_BODY_BYTES = 1_000_000  # 1MB — 연동 스펙 v1.0 (FR-51)

# 긴급(immediate) 분류 — Plan C-1 합의 4기준 중 ①②
URGENT_TYPES = {"REVIEW_CRITERIA", "TARIFF"}
URGENT_WINDOW_DAYS = 7
ALWAYS_URGENT_TYPES = {"NARCOTIC"}


class FeeLookupService:
    """시점 단가 조회 — 전 모듈의 단일 창구 (design §4.4).

    경계 규칙: valid_from <= at_date < valid_to (valid_to NULL = 현행).
    시행일 당일은 신 버전 적용 (FR-35 ① inclusive/exclusive 확정).
    """

    async def get_price(self, session: AsyncSession, *, code: str, code_type: str,
                        at_date: date) -> FeeMaster:
        row = (await session.execute(
            select(FeeMaster)
            .where(FeeMaster.code == code,
                   FeeMaster.code_type == code_type,
                   FeeMaster.valid_from <= at_date)
            .where((FeeMaster.valid_to.is_(None)) | (FeeMaster.valid_to > at_date))
            .order_by(FeeMaster.valid_from.desc())
            .limit(1)
        )).scalar_one_or_none()
        if row is None:
            raise FeeNotFoundError(code, at_date.isoformat())
        return row


class NoticeService:
    """공시 등록/개정/폐지 + AI 발신 큐 적재 (FR-51/52/53)."""

    async def create(self, session: AsyncSession, *, notice_type: str, title: str,
                     body_text: str, effective_from: date, effective_to: date | None,
                     created_by: int, urgent_flag: bool = False) -> Notice:
        self._validate_body(body_text, effective_from)
        notice = Notice(notice_type=notice_type, current_rev=1)
        session.add(notice)
        await session.flush()
        rev = self._add_revision(session, notice, 1, title, body_text,
                                 effective_from, effective_to, created_by)
        await self._enqueue(session, notice, rev, "CREATE", urgent_flag)
        return notice

    async def revise(self, session: AsyncSession, *, erp_ref_id: uuid.UUID, title: str,
                     body_text: str, effective_from: date, effective_to: date | None,
                     created_by: int, urgent_flag: bool = False) -> NoticeRevision:
        self._validate_body(body_text, effective_from)
        notice = await self._get(session, erp_ref_id)
        notice.current_rev += 1
        rev = self._add_revision(session, notice, notice.current_rev, title, body_text,
                                 effective_from, effective_to, created_by)
        await self._enqueue(session, notice, rev, "UPDATE", urgent_flag)
        return rev

    async def revoke(self, session: AsyncSession, *, erp_ref_id: uuid.UUID,
                     created_by: int) -> None:
        """폐지 — REPEALED 전이는 revoke 이벤트 필수 발신 (연동 스펙 v1.0 ②)."""
        notice = await self._get(session, erp_ref_id)
        current = (await session.execute(
            select(NoticeRevision).where(
                NoticeRevision.erp_ref_id == erp_ref_id,
                NoticeRevision.revision_no == notice.current_rev)
        )).scalar_one()
        current.status = "REPEALED"
        await self._enqueue(session, notice, current, "REVOKE", urgent_flag=False,
                            skip_filter=True)  # 폐지는 본문 미발신 — 필터 불요

    # ── 내부 ────────────────────────────────────────────────
    def _validate_body(self, body_text: str, effective_from: date | None) -> None:
        if effective_from is None:
            raise ValidationError("시행일(effective_from)은 필수입니다")  # FR-51 등록 차단
        if len(body_text.encode("utf-8")) > MAX_BODY_BYTES:
            raise ValidationError("본문이 1MB를 초과합니다 — 별표·서식은 첨부로 분리하세요")

    def _add_revision(self, session: AsyncSession, notice: Notice, rev_no: int, title: str,
                      body_text: str, effective_from: date, effective_to: date | None,
                      created_by: int) -> NoticeRevision:
        normalized = ch.normalize_body(body_text)
        rev = NoticeRevision(
            erp_ref_id=notice.erp_ref_id, revision_no=rev_no, title=title,
            body_text=normalized, content_hash=ch.content_hash(body_text),
            effective_from=effective_from, effective_to=effective_to,
            status="ACTIVE" if effective_from <= date.today() else "SCHEDULED",
            created_by=created_by,
        )
        session.add(rev)
        return rev

    def _urgency(self, notice: Notice, rev: NoticeRevision, urgent_flag: bool) -> str:
        """긴급 기준 (Plan C-1): ①7일 내 심사기준·수가 ②마약류 전체 ③즉일 시행 ④수동 플래그."""
        if urgent_flag or notice.notice_type in ALWAYS_URGENT_TYPES:
            return "IMMEDIATE"
        days_to_effective = (rev.effective_from - date.today()).days
        if notice.notice_type in URGENT_TYPES and days_to_effective <= URGENT_WINDOW_DAYS:
            return "IMMEDIATE"
        if days_to_effective <= 0:
            return "IMMEDIATE"
        return "BATCH"

    async def _enqueue(self, session: AsyncSession, notice: Notice, rev: NoticeRevision,
                       event_type: str, urgent_flag: bool, skip_filter: bool = False) -> None:
        """FR-53 필터 게이트 통과 후 큐 적재 — CREATE/UPDATE/REVOKE 모든 이벤트 적용.

        INTERNAL 분류는 무조건 관리자 승인 (BLOCKED 시작).
        승인은 content_hash에 바인딩 — 발신 워커가 hash 불일치 시 재필터 (design §5.3).
        """
        entry = AiDispatchQueue(
            erp_ref_id=notice.erp_ref_id, revision_no=rev.revision_no,
            event_type=event_type, urgency=self._urgency(notice, rev, urgent_flag),
        )
        if skip_filter:
            entry.filter_status = "PASSED"
        elif notice.notice_type == "INTERNAL":
            entry.filter_status = "BLOCKED"
            entry.filter_detail = {"reason": "INTERNAL — 명시적 승인 필요"}
        else:
            charts, emps = await filter_gate.pii_reference_sets(session)   # 실데이터 대조 주입
            result = filter_gate.scan(rev.body_text, known_chart_nos=charts,
                                      known_employee_nos=emps)
            if result.blocked:
                entry.filter_status = "BLOCKED"
                entry.filter_detail = {"findings": result.findings}
            else:
                entry.filter_status = "PASSED"
                entry.approved_hash = rev.content_hash
        session.add(entry)

    async def _get(self, session: AsyncSession, erp_ref_id: uuid.UUID) -> Notice:
        notice = (await session.execute(
            select(Notice).where(Notice.erp_ref_id == erp_ref_id)
        )).scalar_one_or_none()
        if notice is None:
            raise ValidationError("존재하지 않는 공시입니다", {"erp_ref_id": str(erp_ref_id)})
        return notice
