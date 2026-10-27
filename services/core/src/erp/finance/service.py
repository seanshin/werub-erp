"""VoucherService — 전표 생성 단일 창구 (FR-49, design §4.4 UoW).

규약: session 첫 인자 필수, 트랜잭션은 호출자 소유 (자체 commit 금지).
멱등: (source_module, source_ref) UNIQUE — 동일 출처 재호출 시 기존 전표 반환.
"""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import FiscalPeriod, Voucher, VoucherLine

# 거액 수기 전표 상위승인 임계(원) — 이상은 ADMIN/DIRECTOR만 승인(materiality 통제·향후 설정화 가능).
LARGE_VOUCHER_THRESHOLD = 50_000_000
_VOUCHER_DOC = "VOUCHER"     # ApprovalEngine 문서유형 — opt-in 다단계 결재선(룰 있으면 활성)
SOURCE_REF_MAX = 64          # finance.vouchers.source_ref VARCHAR(64) — 멱등키 길이 상한


def _check_source_ref(source_ref: str) -> None:
    """멱등키 길이 가드 (2026-10-09 시나리오 S-A).

    PG는 64자 초과 source_ref를 StringDataRightTruncationError로 거부(→500·이벤트 DEAD)하지만 sqlite는
    길이를 무시해 테스트가 통과했다(HIS UUID로 만든 환불 키 102~116자). 어느 DB에서든 같은 위치에서
    원인이 보이게 전기 전에 막는다 — 키를 만드는 쪽이 64자 안으로 줄여야 한다."""
    if source_ref is None or len(source_ref) > SOURCE_REF_MAX:
        raise ValidationError(
            f"전표 출처키(source_ref)가 {SOURCE_REF_MAX}자를 넘습니다 — 출처키를 만드는 연동/모듈이 "
            f"키를 줄여야 합니다", {"length": len(source_ref or ""), "source_ref": (source_ref or "")[:80]})


@dataclass
class VoucherLineDTO:
    account_code: str
    side: str            # DEBIT | CREDIT
    amount: float
    department_id: int | None = None
    detail: str | None = None
    cost_center: str | None = None   # 라인 확장 차원(G9) — 관리회계 코스트센터
    project: str | None = None        # 라인 확장 차원(G9) — 프로젝트 코드


async def invalidate_balance_checkpoints(session: AsyncSession, *, voucher_on: date) -> int:
    """소급 전기 시 잔액 체크포인트 무효화 (2026-10-09 전체 흐름 검토 D·HIGH-2).

    체크포인트는 `직전 체크포인트 + 증분`으로 쌓이는데, 이미 적재된 경계(through) **이전 날짜**로
    나중에 전기되는 라인(결산조정·외화환산·대손·승인 지연된 수기전표)은 어떤 증분에도 잡히지 않아
    재무상태표가 **영구히 조용히** 틀렸다. 전기 시점에 voucher_on 이후 경계의 체크포인트를 지워
    재무상태표가 즉시 이전 체크포인트(또는 전구간)로 폴백하게 한다. 체크포인트는 캐시라 지워도 정보
    손실이 없고(다음 배치가 재적재), 대부분의 전기는 당월이라 max(through) 한 번 조회로 끝난다.
    배치와의 경합(배치가 미커밋 라인을 못 본 채 체크포인트 적재)은 배치 자기치유가 잡는다."""
    from sqlalchemy import delete, func

    from erp.finance.models import AccountBalanceCheckpoint
    latest = await session.scalar(select(func.max(AccountBalanceCheckpoint.through)))
    if latest is None or voucher_on > latest:
        return 0
    res = await session.execute(delete(AccountBalanceCheckpoint).where(
        AccountBalanceCheckpoint.through >= voucher_on)
        .execution_options(synchronize_session=False))
    return res.rowcount or 0


class VoucherService:
    async def create(self, session: AsyncSession, *, source_module: str, source_ref: str,
                     voucher_on: date, lines: list[VoucherLineDTO], memo: str = "",
                     created_by: int | None = None, allow_closed: bool = False) -> Voucher:
        """전표 생성 — 차대 균형 검증 + 멱등. allow_closed=결산분개 등 시스템 전기(마감 가드 우회)."""
        _check_source_ref(source_ref)
        existing = (await session.execute(
            select(Voucher).where(Voucher.source_module == source_module,
                                  Voucher.source_ref == source_ref)
        )).scalar_one_or_none()
        if existing is not None:
            return existing  # 멱등 — 중복 메시지/재처리 안전 (design §5.2)

        # 기간 마감 가드 — CLOSED 기간엔 전기 불가(역분개는 현재 개방기간으로 전기되어 통과)
        ym = f"{voucher_on.year:04d}-{voucher_on.month:02d}"
        if not allow_closed and (await session.scalar(
                select(FiscalPeriod.status).where(FiscalPeriod.yyyymm == ym))) == "CLOSED":
            raise ValidationError(f"마감된 기간({ym})에는 전표를 생성할 수 없습니다 (정정은 역분개)")

        # 분석 M3 — float 동등 비교 금지: Decimal로 정확 비교 (전표 정합성 §1.1)
        from decimal import Decimal
        debit = sum(Decimal(str(line.amount)) for line in lines if line.side == "DEBIT")
        credit = sum(Decimal(str(line.amount)) for line in lines if line.side == "CREDIT")
        if not lines or debit != credit:
            raise ValidationError("차변·대변 불일치",
                                  {"debit": float(debit), "credit": float(credit)})

        voucher = Voucher(voucher_on=voucher_on, source_module=source_module,
                          source_ref=source_ref, memo=memo, created_by=created_by)
        session.add(voucher)
        await session.flush()
        for line in lines:
            session.add(VoucherLine(voucher_id=voucher.id, account_code=line.account_code,
                                    side=line.side, amount=line.amount,
                                    department_id=line.department_id, detail=line.detail,
                                    cost_center=line.cost_center, project=line.project))
        await invalidate_balance_checkpoints(session, voucher_on=voucher_on)
        # 감사귀속(G3·G5) — 시스템/자동 전기(감가·마감·sync)도 원장 생성 감사에 남긴다.
        # created_by가 None(시스템)이어도 source·금액·마감우회 여부를 불변 audit_log에 기록.
        from erp.audit.service import AuditService
        await AuditService().record_audit(
            session, actor=created_by, action="VOUCHER_CREATE", entity="finance.vouchers",
            target_id=str(voucher.id),
            after={"source_module": source_module, "source_ref": source_ref,
                   "amount": float(debit), "allow_closed": allow_closed})
        return voucher

    @staticmethod
    def _assert_balanced(lines: list) -> None:
        """차대 균형 검증 — Decimal 정확 비교(float 동등비교 금지)."""
        from decimal import Decimal
        debit = sum(Decimal(str(ln.amount)) for ln in lines if ln.side == "DEBIT")
        credit = sum(Decimal(str(ln.amount)) for ln in lines if ln.side == "CREDIT")
        if not lines or debit != credit:
            raise ValidationError("차변·대변 불일치",
                                  {"debit": float(debit), "credit": float(credit)})

    async def create_draft(self, session: AsyncSession, *, source_ref: str, voucher_on: date,
                           lines: list[VoucherLineDTO], memo: str = "",
                           created_by: int | None = None) -> Voucher:
        """수동 전표 초안 (M2 maker-checker) — status=DRAFT, **원장 라인 미생성**(draft_lines 보관).

        DRAFT/PENDING은 VoucherLine이 없어 모든 집계(재무제표·예산·마감)에서 자연 제외 →
        승인(approve) 시점에만 전기. 차대 균형은 초안 단계에서 선검증(조기 오류)."""
        _check_source_ref(source_ref)
        self._assert_balanced(lines)
        voucher = Voucher(
            voucher_on=voucher_on, source_module="manual", source_ref=source_ref,
            memo=memo, created_by=created_by, status="DRAFT",
            draft_lines=[{"account_code": ln.account_code, "side": ln.side,
                          "amount": float(ln.amount), "department_id": ln.department_id,
                          "detail": ln.detail, "cost_center": ln.cost_center,
                          "project": ln.project} for ln in lines])
        session.add(voucher)
        await session.flush()
        return voucher

    async def submit(self, session: AsyncSession, *, voucher_id: int,
                     actor: int | None = None) -> Voucher:
        """초안 상신 — DRAFT→PENDING."""
        # 행 잠금(approve와 같은 순서) — reject·submit이 잠그지 않으면 approve가 전기·POSTED 커밋한 뒤
        # 무조건 UPDATE로 '원장 라인 있는 REJECTED' 전표가 생겼다(2차 검토 D·HIGH-1 반쪽 수정 보완).
        v = (await session.execute(
            select(Voucher).where(Voucher.id == voucher_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if v is None:
            raise ValidationError("존재하지 않는 전표입니다")
        if v.status != "DRAFT":
            raise ValidationError(f"상신 불가 상태: {v.status} (DRAFT만 상신)")
        from datetime import datetime, timezone
        v.status, v.submitted_by = "PENDING", actor
        v.submitted_at = datetime.now(timezone.utc)
        # 2026-10-09 시나리오 S-E D1: 결재선을 **상신 시점**에 만든다 — 종전엔 approve() 안에서만
        # build해, 첫 승인 시도 전까지 인스턴스가 없어 1단계가 결재함(/approvals/worklist)·알림 어디에도
        # 안 떴다(결재자가 알 길이 없어 아무도 승인을 시작하지 못함). 룰 없으면 None(레거시 단일 승인).
        from erp.common.approval_engine import ApprovalEngine
        await ApprovalEngine().build(
            session, doc_type=_VOUCHER_DOC, doc_id=v.id, amount=self._debit_total(v),
            created_by=v.created_by or actor or 0, department_id=self._draft_dept(v))
        return v

    @staticmethod
    def _debit_total(v: Voucher) -> float:
        return sum(float(ln.get("amount", 0)) for ln in (v.draft_lines or [])
                   if ln.get("side") == "DEBIT")

    @staticmethod
    def _draft_dept(v: Voucher) -> int | None:
        return next((ln.get("department_id") for ln in (v.draft_lines or [])
                     if ln.get("department_id")), None)

    async def approve(self, session: AsyncSession, *, voucher_id: int, actor: int | None = None,
                      actor_role: str | None = None, override: bool = False) -> Voucher:
        """승인 = 전기 — PENDING→POSTED. **작성자≠승인자(SoD)** 강제, 승인 시 원장 라인 생성.

        opt-in 다단계: VOUCHER 결재룰 있으면 결재선 라우팅(현 단계 승인·완주 시 전기·미완주 시 PENDING 유지),
        없으면 레거시 단일 승인 + 거액(≥임계) materiality 게이트(ADMIN/DIRECTOR). 마감 가드·차대 균형 재검증."""
        # 전표 행잠금 + 최신값 재적재 (2026-10-09 전체 흐름 검토 D·HIGH-1) — 종전엔 session.get이라
        # 동시 승인(더블클릭·재시도·두 결재자) 두 트랜잭션이 같은 PENDING 스냅샷을 읽고 둘 다
        # VoucherLine을 INSERT해 **이중 전기**됐다. 다단계 경로도 같은 구멍: 첫 호출이 마지막 단계를
        # 승인·전기한 뒤 두 번째 호출은 PENDING 인스턴스가 없으니 build()가 **새 결재선**을 만들고
        # 다시 전기했다. 행잠금으로 직렬화하고, 대기 후에는 커밋된 status(POSTED)를 다시 읽어
        # 아래 PENDING 검사에서 막는다. populate_existing=identity map의 낡은 PENDING 객체를 덮어쓴다.
        v = (await session.execute(
            select(Voucher).where(Voucher.id == voucher_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if v is None:
            raise ValidationError("존재하지 않는 전표입니다")
        if v.status != "PENDING":
            raise ValidationError(f"승인 불가 상태: {v.status} (PENDING만 승인)")
        if actor is not None and actor in (v.created_by, v.submitted_by):
            raise ValidationError("작성자·상신자는 본인 전표를 승인할 수 없습니다 (직무분리)")
        debit_total = self._debit_total(v)
        ym = f"{v.voucher_on.year:04d}-{v.voucher_on.month:02d}"
        if (await session.scalar(
                select(FiscalPeriod.status).where(FiscalPeriod.yyyymm == ym))) == "CLOSED":
            raise ValidationError(f"마감된 기간({ym})에는 전기할 수 없습니다 (정정은 역분개)")
        # opt-in 다단계 결재선 — VOUCHER 룰 있으면 인스턴스(멱등), 없으면 None(레거시).
        from erp.common.approval_engine import ApprovalEngine
        engine = ApprovalEngine()
        inst = await engine.build(session, doc_type=_VOUCHER_DOC, doc_id=v.id, amount=debit_total,
                                  created_by=v.created_by or v.submitted_by or 0,
                                  department_id=self._draft_dept(v))
        if inst is not None:
            out = await engine.act(session, doc_type=_VOUCHER_DOC, doc_id=v.id, actor=actor,
                                   approve=True, override=override)
            if out.status != "APPROVED":
                return v          # 다음 단계 대기 — 미전기(PENDING 유지)
        elif (debit_total >= LARGE_VOUCHER_THRESHOLD           # 룰 없음(레거시) → materiality 게이트
              and actor_role is not None and actor_role not in ("ADMIN", "DIRECTOR")):
            raise ValidationError(
                f"거액 전표(≥{LARGE_VOUCHER_THRESHOLD:,}원)는 관리자(ADMIN)·원장(DIRECTOR) 승인이 필요합니다",
                {"amount": debit_total})
        lines = [VoucherLineDTO(**ln) for ln in (v.draft_lines or [])]
        self._assert_balanced(lines)
        for ln in lines:
            session.add(VoucherLine(voucher_id=v.id, account_code=ln.account_code,
                                    side=ln.side, amount=ln.amount,
                                    department_id=ln.department_id, detail=ln.detail,
                                    cost_center=ln.cost_center, project=ln.project))
        await invalidate_balance_checkpoints(session, voucher_on=v.voucher_on)
        from datetime import datetime, timezone
        v.status, v.approved_by = "POSTED", actor
        v.approved_at = datetime.now(timezone.utc)
        return v

    async def reject(self, session: AsyncSession, *, voucher_id: int, reason: str,
                     actor: int | None = None) -> Voucher:
        """반려 — PENDING→REJECTED (사유 필수)."""
        if not (reason or "").strip():
            raise ValidationError("반려 사유는 필수입니다")
        # 행 잠금(approve와 같은 순서) — reject·submit이 잠그지 않으면 approve가 전기·POSTED 커밋한 뒤
        # 무조건 UPDATE로 '원장 라인 있는 REJECTED' 전표가 생겼다(2차 검토 D·HIGH-1 반쪽 수정 보완).
        v = (await session.execute(
            select(Voucher).where(Voucher.id == voucher_id).with_for_update()
            .execution_options(populate_existing=True))).scalar_one_or_none()
        if v is None:
            raise ValidationError("존재하지 않는 전표입니다")
        if v.status != "PENDING":
            raise ValidationError(f"반려 불가 상태: {v.status} (PENDING만 반려)")
        # 진행 중 다단계 결재선이 있으면 단계 반려로 기록(직무분리·이력), 없으면 레거시 즉시 반려.
        from erp.common.approval_engine import ApprovalEngine
        engine = ApprovalEngine()
        inst = await engine.get(session, doc_type=_VOUCHER_DOC, doc_id=voucher_id)
        if inst is not None and inst.get("status") == "PENDING":
            await engine.act(session, doc_type=_VOUCHER_DOC, doc_id=voucher_id, actor=actor,
                             approve=False, note=reason)
        v.status, v.reject_reason, v.approved_by = "REJECTED", reason, actor
        return v

    async def reverse(self, session: AsyncSession, *, voucher_id: int, reason: str,
                      created_by: int | None = None) -> Voucher:
        """역분개 (FR-54) — 원 전표 보존 + 반대 분개 신규 생성."""
        original = (await session.execute(
            select(Voucher).where(Voucher.id == voucher_id))).scalar_one_or_none()
        if original is None:
            raise ValidationError("존재하지 않는 전표입니다")
        if original.status == "REVERSED":
            raise ValidationError("이미 역분개된 전표입니다")

        lines = (await session.execute(
            select(VoucherLine).where(VoucherLine.voucher_id == voucher_id))).scalars().all()
        flipped = [VoucherLineDTO(
            account_code=line.account_code,
            side="CREDIT" if line.side == "DEBIT" else "DEBIT",
            amount=float(line.amount), department_id=line.department_id,
            detail=f"역분개: {line.detail or ''}",
            cost_center=line.cost_center, project=line.project) for line in lines]

        # 2026-10-09 시나리오 S-E D3: 외화 기말환산 전표는 **원 전표일자로** 역분개한다. 환산은 '그 기말
        # 장부가 대비 환산차'라, 오늘 날짜로 역분개하면 재실행(post)이 원 전표를 포함한 장부가로 재측정해
        # 당해 연도는 맞지만 다음 연도 GL이 원 환산차만큼 과소·역분개월에 4650/5670 잔액이 떠 있었다.
        # 원일자 역분개면 그 기말 장부가에서 원 전표가 상계돼 재실행 환산차가 그대로 맞는다.
        reverse_on = date.today()
        if (original.source_module == "closing-ifrs"
                and (original.source_ref or "").startswith("fx-translate:")):
            reverse_on = original.voucher_on
            ym = f"{reverse_on.year:04d}-{reverse_on.month:02d}"
            if (await session.scalar(select(FiscalPeriod.status).where(
                    FiscalPeriod.yyyymm == ym))) == "CLOSED":
                raise ValidationError(
                    f"외화환산 전표는 원 전표일자({reverse_on})로 역분개해야 합니다 — 마감된 기간({ym})이라 "
                    f"재무 승인권자가 기간을 재개방한 뒤 역분개·재환산하세요",
                    {"voucher_id": voucher_id, "period": ym})
        # 역분개 키 = 원 키 + 접미 — 원 키가 길면 64자를 넘어 PG에서 역분개 자체가 실패하므로 원 키
        # 앞부분만 남긴다(접미의 원 전표 id가 유일성을 보장·2026-10-09 시나리오 S-A 길이 가드 동반).
        suffix = f"/reversal-{voucher_id}"
        rev_ref = f"{original.source_ref[:SOURCE_REF_MAX - len(suffix)]}{suffix}"
        reversal = await self.create(
            session, source_module=original.source_module,
            source_ref=rev_ref,
            voucher_on=reverse_on, lines=flipped,
            memo=f"역분개 ({reason}) — 원 전표 #{voucher_id}", created_by=created_by)
        reversal.reverses = original.id
        original.status = "REVERSED"
        original.reversed_by = reversal.id
        return reversal
