"""연말 결산분개 — 손익(수익·비용)을 이익잉여금으로 대체. 회계연도 마감(FR-08 확장).

수익(대변잔액)→차변 상계, 비용(차변잔액)→대변 상계, 순액→이익잉여금(3300). 결산일=YYYY-12-31.
source_ref=closing:{year} 멱등. 마감 기간이어도 전기(allow_closed) — 결산 후 재집계 시 손익→0.
"""

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError
from erp.finance.models import Account, Voucher, VoucherLine
from erp.finance.service import VoucherLineDTO, VoucherService

ACC_RETAINED = "3300"   # 이익잉여금


class ClosingService:
    def __init__(self) -> None:
        self._vs = VoucherService()

    async def close_year(self, session: AsyncSession, *, fiscal_year: int,
                         user_id: int | None = None) -> dict:
        """회계연도 결산 — 연도별 직렬화 + 동시 호출 경합 패자의 멱등 수습.

        2026-10-09 시나리오 S-E D2: 동시 `POST /finance/closing/{year}` 두 건이 같은 스냅샷(결산 전표 없음
        또는 보충 seq=count+1)을 보고 같은 source_ref를 INSERT → uq_voucher_source IntegrityError **500**.
        PG는 연도별 advisory xact lock으로 "조회→집계→전기"를 직렬화하고(뒤 호출은 앞 호출 커밋분을 보고
        already_closed), 그래도 유니크 위반이 나면(잠금 밖 경로) savepoint 롤백 후 한 번 재평가한다."""
        from sqlalchemy.exc import IntegrityError

        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"closing:{fiscal_year}")
        try:
            async with session.begin_nested():
                return await self._close_once(session, fiscal_year=fiscal_year, user_id=user_id)
        except IntegrityError:
            pass        # 경합 패자 — 승자의 커밋분을 보고 재평가(대개 already_closed)
        try:
            async with session.begin_nested():
                return await self._close_once(session, fiscal_year=fiscal_year, user_id=user_id)
        except IntegrityError as exc:
            raise ValidationError(
                f"{fiscal_year}년 결산이 동시에 진행 중입니다 — 잠시 후 결산 상태를 다시 확인하세요") from exc

    async def _close_once(self, session: AsyncSession, *, fiscal_year: int,
                          user_id: int | None = None) -> dict:
        """회계연도 결산 — 손익계정 잔액을 이익잉여금으로 이전(결산분개).

        이미 결산된 연도면 **보충 결산분개**(closing:{year}:{n})로 결산 후 그 연도에 전기된 손익만 이전.
        아래 집계는 결산분개(source_module=closing)를 포함하므로 결산 후 잔여 손익 = 결산 이후 전기분.
        잔여가 0이면 멱등(기존 결산 전표 반환)."""
        # 2026-10-09 전체 흐름 검토 D·MED-3 — 종전엔 closing:{year}가 있으면 무조건 반환(1회성)이라,
        # 결산 뒤 그 회계연도로 allow_closed 전기되는 손익(이연법인세·외화환산·대손 조정 등)이 이익잉여금으로
        # **영영 대체되지 않았다**(재무상태표 자본에 손익 잔액이 떠 있음). 재호출 시 잔여분을 보충 결산한다.
        existing = await session.scalar(select(Voucher).where(
            Voucher.source_module == "closing", Voucher.source_ref == f"closing:{fiscal_year}"))
        from erp.finance.fiscal import fiscal_range
        since, until = await fiscal_range(session, fiscal_year)   # 기관 회계연도(기본 역년)
        # 회계연도 손익계정별 차/대 합계 (REVENUE·EXPENSE)
        rows = (await session.execute(
            select(VoucherLine.account_code, Account.kind,
                   func.coalesce(func.sum(
                       case((VoucherLine.side == "DEBIT", VoucherLine.amount), else_=0)), 0),
                   func.coalesce(func.sum(
                       case((VoucherLine.side == "CREDIT", VoucherLine.amount), else_=0)), 0))
            .join(Voucher, Voucher.id == VoucherLine.voucher_id)
            .join(Account, Account.code == VoucherLine.account_code)
            .where(Voucher.status.in_(("POSTED", "REVERSED")),   # 역분개 원(REVERSED)+반전(POSTED) 상계 — 손익계산서 계약과 동일(POSTED만이면 반전된 수익/비용이 결산·이익잉여금 왜곡)
                   Account.kind.in_(("REVENUE", "EXPENSE")),
                   Voucher.voucher_on >= since, Voucher.voucher_on <= until)
            .group_by(VoucherLine.account_code, Account.kind))).all()

        lines: list[VoucherLineDTO] = []
        total_rev = total_exp = 0.0
        for code, kind, debit, credit in rows:
            d, c = float(debit), float(credit)
            if kind == "REVENUE":
                bal = c - d                      # 수익 대변잔액 → 상계: 차변 전기
                total_rev += bal
                if round(bal) > 0:
                    lines.append(VoucherLineDTO(code, "DEBIT", bal))
                elif round(bal) < 0:
                    lines.append(VoucherLineDTO(code, "CREDIT", -bal))
            else:
                bal = d - c                      # 비용 차변잔액 → 상계: 대변 전기
                total_exp += bal
                if round(bal) > 0:
                    lines.append(VoucherLineDTO(code, "CREDIT", bal))
                elif round(bal) < 0:
                    lines.append(VoucherLineDTO(code, "DEBIT", -bal))

        net = total_rev - total_exp              # 당기순이익
        if existing is not None and not lines:
            # 결산 후 추가 손익 없음 — 멱등(가장 최근 결산 전표 반환)
            last = await session.scalar(select(Voucher).where(
                Voucher.source_module == "closing",
                Voucher.source_ref.like(f"closing:{fiscal_year}:%"))
                .order_by(Voucher.id.desc()).limit(1))
            return {"fiscal_year": fiscal_year, "voucher_id": (last or existing).id,
                    "already_closed": True}
        if round(net) == 0 and not lines:
            raise ValidationError(f"{fiscal_year}년 결산 대상 손익이 없습니다")
        # 순이익→이익잉여금 대변, 순손실→차변
        if round(net) > 0:
            lines.append(VoucherLineDTO(ACC_RETAINED, "CREDIT", net))
        elif round(net) < 0:
            lines.append(VoucherLineDTO(ACC_RETAINED, "DEBIT", -net))

        if existing is None:
            ref, memo = (f"closing:{fiscal_year}",
                         f"{fiscal_year} 회계연도 결산분개 (당기순이익 {round(net):,})")
        else:
            seq = (await session.scalar(select(func.count()).select_from(Voucher).where(
                Voucher.source_module == "closing",
                Voucher.source_ref.like(f"closing:{fiscal_year}:%")))) + 1
            ref, memo = (f"closing:{fiscal_year}:{seq}",
                         f"{fiscal_year} 회계연도 보충 결산분개 #{seq} — 결산 후 전기 손익 {round(net):,}")
        v = await self._vs.create(
            session, source_module="closing", source_ref=ref,
            voucher_on=until, lines=lines, created_by=user_id, allow_closed=True, memo=memo)
        out = {"fiscal_year": fiscal_year, "voucher_id": v.id,
               "total_revenue": round(total_rev), "total_expense": round(total_exp),
               "net_income": round(net)}
        if existing is not None:
            out.update(supplementary=True, original_voucher_id=existing.id)
        return out
