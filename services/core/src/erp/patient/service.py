"""Patient 유스케이스 — 환자(최소화)·접수·수납·일마감(F1)·마감 후 정정(FR-54)."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common import crypto
from erp.errors import ValidationError
from erp.finance.service import VoucherLineDTO, VoucherService
from erp.patient.models import DailyClosing, Patient, Payment, Reception, Refund
from erp.common.kst import kst_iso

_audit = AuditService()

# 수납 전표 계정 (계정과목 마스터와 일치 — seed). HIS 일마감(inbound_consumer)과 동일 매핑.
ACC_CASH = "1110"      # 현금성 자산 (현금·계좌이체)
ACC_CARD_AR = "1132"   # 카드미수금 (일반 미수금 1130과 분리 — aging·카드사 입금대사 구분)
ACC_REVENUE = "4110"   # 외래 수익
ACC_PATIENT_AR = "1131"  # 환자미수금 (발생주의 — 진료시점 발생, 수납시점 상계)


_CARD_METHODS = {"CARD", "CREDIT", "CREDITCARD", "CREDIT_CARD", "DEBIT", "DEBIT_CARD",
                 "체크", "카드", "신용", "신용카드"}


def _is_card(method: str | None) -> bool:
    """카드 결제수단 판정 — 정확일치 'CARD'만 보던 것을 변형(CREDIT/카드 등)까지 정규화.

    HIS 인입·오타로 'CREDITCARD'/'카드' 등이 들어와도 카드매출이 현금(1110)으로 오전기되어
    카드사 입금대사(1132)에서 누락되고 현금시재가 과대되던 오분류 방지."""
    m = (method or "").upper()
    return m in _CARD_METHODS or "CARD" in m


def _revenue_lines(by_method: dict[str, float], *,
                   accrual: bool = False) -> tuple[list[VoucherLineDTO], float]:
    """수단별 차변 분개 — 카드→카드미수(1132), 그 외(CASH/TRANSFER)→현금(1110).

    대변: 현금주의(기본)=외래수익(4110, 수납시점 수익인식) / 발생주의(accrual)=환자미수금(1131) 상계
    (수익은 진료시점 receivable_service.accrue에서 이미 인식 → 이중계상 방지).
    Payment.amount는 정수(원) Numeric(14,0)이라 라운딩 손실 없음 → 차/대변 정합 보장.
    """
    card = sum(a for m, a in by_method.items() if _is_card(m))
    cash = sum(a for m, a in by_method.items() if not _is_card(m))
    lines: list[VoucherLineDTO] = []
    if cash:
        lines.append(VoucherLineDTO(ACC_CASH, "DEBIT", round(cash)))
    if card:
        lines.append(VoucherLineDTO(ACC_CARD_AR, "DEBIT", round(card)))
    total = round(cash) + round(card)
    if total > 0:
        lines.append(VoucherLineDTO(ACC_PATIENT_AR if accrual else ACC_REVENUE, "CREDIT", total))
    return lines, total


class PatientService:
    async def create(self, session: AsyncSession, *, chart_no: str, name: str,
                     birth_date: date, phone: str | None = None) -> Patient:
        """FR-33 — 4필드만. 주민번호 입력 경로 자체가 없음."""
        p = Patient(chart_no=chart_no, name=name, birth_date=birth_date)
        session.add(p)
        await session.flush()
        if phone:
            p.phone_enc = crypto.encrypt_column(
                phone, aad=crypto.column_aad("patient.patients.phone", p.id))
            p.phone_bidx = crypto.phone_blind_index(phone)
        return p

    async def search(self, session: AsyncSession, *, name: str | None = None,
                     birth_date: date | None = None, chart_no: str | None = None,
                     phone: str | None = None, limit: int = 50,
                     dept_codes: list[str] | None = None) -> list[Patient]:
        """검색 (FR-15) — 전화번호는 blind index 전체 일치만 (§6.4).

        dept_codes 지정 시 본부서 접수(Reception) 이력 환자만(부서 경계 §6.2).
        """
        q = select(Patient).limit(min(limit, 200))
        if dept_codes is not None:     # 부서 경계 — 본부서 접수 환자로 한정(빈 후보=없음, 과차단 안전)
            from erp.patient.models import Reception
            q = q.where(Patient.id.in_(
                select(Reception.patient_id).where(Reception.dept_code.in_(dept_codes))))
        if chart_no:
            q = q.where(Patient.chart_no.ilike(f"%{chart_no}%"))   # 부분 일치
        if name:
            q = q.where(Patient.name.ilike(f"%{name}%"))           # 부분 일치(검색 UX)
        if birth_date:
            q = q.where(Patient.birth_date == birth_date)
        if phone:
            q = q.where(Patient.phone_bidx == crypto.phone_blind_index(phone))
        return list((await session.execute(q)).scalars().all())


class ReceptionService:
    async def receive(self, session: AsyncSession, *, patient_id: int, dept_code: str,
                      doctor_id: int | None, received_on: date,
                      source: str = "MANUAL") -> Reception:
        patient = (await session.execute(
            select(Patient).where(Patient.id == patient_id))).scalar_one_or_none()
        if patient is None:
            raise ValidationError("존재하지 않는 환자입니다")
        rec = Reception(patient_id=patient_id, dept_code=dept_code,
                        doctor_id=doctor_id, received_on=received_on, source=source)
        session.add(rec)
        patient.last_visit_on = received_on  # 보존연한 기산 (NFR Data Retention)
        await session.flush()
        return rec


async def _lock_closing_day(session: AsyncSession, on: date) -> None:
    """일자 마감 직렬화 — pay↔close_day TOCTOU 방지(PG advisory xact lock·sqlite no-op).

    pay가 _is_closed=false를 본 뒤 close_day가 그 수납을 못 집계해 GL 미전기(마감보고≠GL)되던
    경합을, 같은 일자를 만지는 두 트랜잭션이 이 락으로 직렬화돼 차단(narcotics 채번 락과 동형)."""
    if session.bind.dialect.name == "postgresql":
        await session.execute(
            select(func.pg_advisory_xact_lock(0x50434C53, on.toordinal())))  # 'PCLS'


class PaymentService:
    def __init__(self, vouchers: VoucherService | None = None):
        self._vouchers = vouchers or VoucherService()

    async def pay(self, session: AsyncSession, *, reception_id: int, amount: float,
                  method: str, paid_on: date, created_by: int | None = None) -> Payment:
        if amount <= 0:
            raise ValidationError("수납 금액은 0보다 커야 합니다")
        await _lock_closing_day(session, paid_on)   # 마감과 직렬화 후 마감상태 확정 검사
        closed = await self._is_closed(session, paid_on)
        if closed:
            raise ValidationError("마감된 일자입니다 — 마감 후 정정(amend) 절차를 사용하세요",
                                  {"date": paid_on.isoformat()})  # FR-54 경로 강제
        # 서버 멱등 — 동일 접수·금액·수단·수납일의 PAID 수납이 이미 있으면 중복(더블클릭/재시도)으로
        # 차단(과수납·일계표 왜곡 방지). FE 이중제출 가드(진행 중 비활성화)의 서버 계층 방어심층.
        dup = await session.scalar(select(Payment.id).where(
            Payment.reception_id == reception_id, Payment.amount == round(amount),
            Payment.method == method, Payment.paid_on == paid_on,
            Payment.status == "PAID").limit(1))
        if dup is not None:
            raise ValidationError("이미 동일한 수납이 있습니다 (중복 수납 방지)",
                                  {"reception_id": reception_id, "existing_payment_id": dup})
        rec = await session.get(Reception, reception_id)   # 진료월 귀속 — 접수(received_on) 월
        care_month = rec.received_on.strftime("%Y-%m") if rec is not None else None
        pay = Payment(reception_id=reception_id, amount=amount, method=method, paid_on=paid_on,
                      care_month=care_month,
                      created_by=created_by)   # 입력자(4-eyes) — HIS 인입은 None(시스템)
        session.add(pay)
        await session.flush()
        return pay

    async def _is_closed(self, session: AsyncSession, on: date) -> bool:
        return (await session.execute(
            select(func.count()).select_from(DailyClosing)
            .where(DailyClosing.closing_on == on, DailyClosing.status == "CLOSED")
        )).scalar_one() > 0


class ClosingService:
    """일마감 (FR-18) + 마감 후 정정 (FR-54 — 역분개+재마감, 원 마감 보존)."""

    def __init__(self, vouchers: VoucherService | None = None):
        self._vouchers = vouchers or VoucherService()

    async def _is_self_closing(self, session: AsyncSession, on: date, actor: int) -> bool:
        """직무분리(4-eyes) — 그날 PAID 수납을 actor가 100% 입력했는가.

        타 담당자 입력분 또는 HIS 인입(created_by NULL)이 하나라도 있으면 False(4-eyes 충족).
        수납이 없으면 False(통제 무의미).
        """
        rows = (await session.execute(
            select(Payment.created_by)
            .where(Payment.paid_on == on, Payment.status == "PAID"))).scalars().all()
        return bool(rows) and all(cb == actor for cb in rows)

    async def close_day(self, session: AsyncSession, *, closing_on: date,
                        actor: int, allow_self_close: bool = False) -> DailyClosing:
        await _lock_closing_day(session, closing_on)   # 진행 중 수납과 직렬화 후 집계(TOCTOU 방지)
        if await PaymentService()._is_closed(session, closing_on):
            raise ValidationError("이미 마감된 일자입니다")
        self_close = await self._is_self_closing(session, closing_on, actor)
        if self_close and not allow_self_close:   # 직무분리 — 입력자 단독 마감 차단(force로 강제)
            raise ValidationError(
                "직무분리 — 본인이 입력한 수납만 있어 마감할 수 없습니다 "
                "(다른 담당자 확인 필요, 부득이 시 강제 마감)",
                {"reason": "self_close", "date": closing_on.isoformat()})
        total, count = await self._aggregate(session, closing_on)
        seq = await self._next_seq(session, closing_on)
        closing = DailyClosing(closing_on=closing_on, seq=seq, total_amount=total,
                               payment_count=count, closed_by=actor)
        session.add(closing)
        try:                                  # 부분 유니크(일자당 CLOSED 1건)로 동시 마감 차단
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            raise ValidationError("이미 마감된 일자입니다 (동시 마감 감지)") from None
        if total > 0:
            # F1 — 단일 Tx: 마감 + 전표가 같은 세션 (design §2.2). 수단별 차변 분개.
            # 발생주의 ON이면 대변=환자미수금(1131) 상계, OFF면 외래수익(4110, 현행).
            from erp.config import get_settings
            accrual = get_settings().patient_accrual_enabled
            lines, _ = _revenue_lines(await self._aggregate_by_method(session, closing_on),
                                      accrual=accrual)
            voucher = await self._vouchers.create(
                session, source_module="patient",
                source_ref=f"closing-{closing_on.isoformat()}-{seq}",
                voucher_on=closing_on, lines=lines,
                memo=f"외래 수납 일마감 {closing_on} (#{seq})", created_by=actor)
            closing.voucher_ref = f"voucher-{voucher.id}"
        await _audit.record_audit(session, actor=actor, action="CLOSE",
                                  entity="patient.daily_closings", target_id=str(closing.id),
                                  after={"on": closing_on.isoformat(), "total": float(total),
                                         "self_close": self_close})  # 강제 마감 추적(직무분리 예외)
        return closing

    async def amend(self, session: AsyncSession, *, closing_on: date, actor: int,
                    correction_payments: list[dict], reason: str) -> DailyClosing:
        """FR-54 — 마감 직접 수정 금지: 역분개 → 정정 수납 → 재마감."""
        original = (await session.execute(
            select(DailyClosing).where(DailyClosing.closing_on == closing_on,
                                       DailyClosing.status == "CLOSED")
        )).scalar_one_or_none()
        if original is None:
            raise ValidationError("마감 기록이 없습니다")

        # ① 원 전표 역분개 (원 마감·전표 보존)
        if original.voucher_ref:
            voucher_id = int(original.voucher_ref.removeprefix("voucher-"))
            await self._vouchers.reverse(session, voucher_id=voucher_id,
                                         reason=reason, created_by=actor)
        original.status = "AMENDED"
        await session.flush()   # AMENDED 선반영 — 재마감 CLOSED insert 시 부분 유니크 충돌 방지

        # ② 정정 수납 반영 (취소는 음수 아닌 CANCELED 처리 + 신규 수납)
        for c in correction_payments:
            if c.get("cancel_payment_id"):
                target = (await session.execute(
                    select(Payment).where(Payment.id == c["cancel_payment_id"])
                )).scalar_one_or_none()
                # 취소 대상은 **해당 마감일의 PAID 수납**만 — 타 일자 수납을 취소하면 그 날
                # 마감·전표가 역분개·재집계되지 않아 일자별 원장이 오염됨(IDOR류 정합 결함).
                if target is None:
                    raise ValidationError("취소 대상 수납을 찾을 수 없습니다",
                                          {"payment_id": c["cancel_payment_id"]})
                if target.paid_on != closing_on or target.status != "PAID":
                    raise ValidationError(
                        "해당 마감일의 정상(PAID) 수납만 정정 취소할 수 있습니다",
                        {"payment_id": target.id, "paid_on": target.paid_on.isoformat(),
                         "status": target.status})
                target.status = "CANCELED"
                session.add(Payment(reception_id=target.reception_id,
                                    amount=c.get("new_amount", 0) or 0,
                                    method=target.method, paid_on=closing_on,
                                    cancels_id=target.id, created_by=actor)
                            ) if c.get("new_amount") else None

        # ③ 재마감 (차수 증가)
        total, count = await self._aggregate(session, closing_on)
        seq = await self._next_seq(session, closing_on)
        reclosing = DailyClosing(closing_on=closing_on, seq=seq, total_amount=total,
                                 payment_count=count, closed_by=actor)
        session.add(reclosing)
        try:                                  # 동시 amend/마감 충돌 차단(부분 유니크)
            async with session.begin_nested():
                await session.flush()
        except IntegrityError:
            raise ValidationError("이미 재마감된 일자입니다 (동시 정정 감지)") from None
        if total > 0:
            from erp.config import get_settings
            lines, _ = _revenue_lines(await self._aggregate_by_method(session, closing_on),
                                      accrual=get_settings().patient_accrual_enabled)
            voucher = await self._vouchers.create(
                session, source_module="patient",
                source_ref=f"closing-{closing_on.isoformat()}-{seq}",
                voucher_on=closing_on, lines=lines,
                memo=f"재마감 {closing_on} (#{seq}) — {reason}", created_by=actor)
            reclosing.voucher_ref = f"voucher-{voucher.id}"
        await _audit.record_audit(session, actor=actor, action="AMEND",
                                  entity="patient.daily_closings", target_id=str(reclosing.id),
                                  before={"closing_id": original.id},
                                  after={"reason": reason, "seq": seq})
        return reclosing

    async def preview(self, session: AsyncSession, on: date, *, viewer: int | None = None) -> dict:
        """마감 전 일계표 미리보기 — 그날 PAID 수납 합계·건수·수단별·마감 여부·직무분리 경고."""
        total, count = await self._aggregate(session, on)
        rows = (await session.execute(
            select(Payment.method, func.coalesce(func.sum(Payment.amount), 0), func.count())
            .where(Payment.paid_on == on, Payment.status == "PAID")
            .group_by(Payment.method))).all()
        self_closing = (await self._is_self_closing(session, on, viewer)
                        if viewer is not None else False)
        # 환불 병기 — 환불은 자기 전표(refund-N)로 GL 전기되고 마감 전표엔 미포함이므로,
        # 일계표에 별도 표기해 현금 시재(수납−환불)와 마감보고 간 오해를 차단.
        r_total, r_count = (await session.execute(
            select(func.coalesce(func.sum(Refund.amount), 0), func.count())
            .where(Refund.refunded_on == on))).one()
        # 현금 시재 대사값은 현금성(비카드) 수단만 — 종전 net_cash=전수단 합계는 카드매출이
        # 물리 시재를 부풀리는 오도(계산검토 M-5). 환불도 수단별 분리(_is_card 동일 규약·NULL=현금).
        cash_in = sum(float(a) for m, a, _c in rows if not _is_card(m))
        r_rows = (await session.execute(
            select(Refund.method, func.coalesce(func.sum(Refund.amount), 0))
            .where(Refund.refunded_on == on).group_by(Refund.method))).all()
        r_cash = sum(float(a) for m, a in r_rows if not _is_card(m))
        return {"closing_on": on.isoformat(), "total": total, "count": count,
                "by_method": [{"method": m, "amount": float(a), "count": int(c)} for m, a, c in rows],
                "refund_total": float(r_total), "refund_count": int(r_count),
                "net_total": total - float(r_total),          # 전 수단 순수납(보고용)
                "net_cash": cash_in - r_cash,                  # 현금성(비카드)만 — 물리 시재 대사용
                "already_closed": await PaymentService()._is_closed(session, on),
                "self_closing": self_closing}   # 본인 단독 입력 → 마감 시 강제 필요

    async def tellers_summary(self, session: AsyncSession, on: date) -> dict:
        """수납원별 일일 마감(시재 대사) — 입력자(created_by)별 수납·환불·현금시재.

        마감 전표는 일자 단위 집계라 수납원별 시재 책임이 안 보이던 갭 보완. HIS 자동인입(created_by NULL)은
        'HIS(자동)'로 분리. net_cash(현금성 수납−현금성 환불)가 수납원 물리 시재 대사값."""
        from erp.common.models import User
        from erp.hr.models import Employee
        pay = (await session.execute(
            select(Payment.created_by, Payment.method,
                   func.coalesce(func.sum(Payment.amount), 0), func.count())
            .where(Payment.paid_on == on, Payment.status == "PAID")
            .group_by(Payment.created_by, Payment.method))).all()
        ref = (await session.execute(
            select(Refund.created_by, Refund.method,
                   func.coalesce(func.sum(Refund.amount), 0), func.count())
            .where(Refund.refunded_on == on)
            .group_by(Refund.created_by, Refund.method))).all()
        tellers: dict = {}

        def _slot(uid):
            return tellers.setdefault(uid, {"total": 0.0, "count": 0, "cash": 0.0, "card": 0.0,
                                            "refund": 0.0, "refund_count": 0, "refund_cash": 0.0})
        for uid, method, amt, cnt in pay:
            t = _slot(uid)
            a = float(amt)
            t["total"] += a
            t["count"] += int(cnt)
            t["card" if _is_card(method) else "cash"] += a
        for uid, method, amt, cnt in ref:
            t = _slot(uid)
            a = float(amt)
            t["refund"] += a
            t["refund_count"] += int(cnt)
            if not _is_card(method):
                t["refund_cash"] += a
        uids = [u for u in tellers if u is not None]
        names: dict = {}
        if uids:
            nrows = (await session.execute(
                select(User.id, Employee.name)
                .join(Employee, Employee.id == User.employee_id, isouter=True)
                .where(User.id.in_(uids)))).all()
            names = {uid: nm for uid, nm in nrows}
        items = []
        for uid, t in tellers.items():
            items.append({
                "teller_id": uid,
                "teller": (names.get(uid) or (f"user:{uid}" if uid is not None else "HIS(자동)")),
                "net_total": t["total"] - t["refund"],
                "net_cash": t["cash"] - t["refund_cash"],   # 현금성 시재 대사
                **t})
        items.sort(key=lambda x: x["total"], reverse=True)
        return {"closing_on": on.isoformat(), "tellers": items,
                "total": sum(t["total"] for t in items),
                "net_cash": sum(t["net_cash"] for t in items)}

    async def _aggregate_by_method(self, session: AsyncSession, on: date) -> dict[str, float]:
        """수단별 PAID 수납 합계 — 전표 차변 분개용(현금/카드 분리)."""
        rows = (await session.execute(
            select(Payment.method, func.coalesce(func.sum(Payment.amount), 0))
            .where(Payment.paid_on == on, Payment.status == "PAID")
            .group_by(Payment.method))).all()
        return {m: float(a) for m, a in rows}

    async def history(self, session: AsyncSession, *, yyyymm: str | None = None,
                      limit: int = 100) -> list[dict]:
        """마감 이력 — 최신순(일자·차수 desc). yyyymm='YYYY-MM' 월 필터(날짜 범위, 방언 무관)."""
        q = select(DailyClosing).order_by(
            DailyClosing.closing_on.desc(), DailyClosing.seq.desc()).limit(limit)
        if yyyymm:
            try:
                y, m = (int(x) for x in yyyymm.split("-"))
                start = date(y, m, 1)
                end = date(y + (m == 12), (m % 12) + 1, 1)
            except (ValueError, TypeError) as exc:
                raise ValidationError("yyyymm 형식은 YYYY-MM 입니다") from exc
            q = q.where(DailyClosing.closing_on >= start, DailyClosing.closing_on < end)
        rows = (await session.scalars(q)).all()
        # 일자별 환불 병기 — 마감보고와 현금 시재(수납−환불) 대사용(마감 전표엔 환불 미포함)
        refunds: dict = {}
        if rows:
            days = {c.closing_on for c in rows}
            refunds = {d: (float(t), int(n)) for d, t, n in (await session.execute(
                select(Refund.refunded_on, func.coalesce(func.sum(Refund.amount), 0),
                       func.count())
                .where(Refund.refunded_on.in_(days))
                .group_by(Refund.refunded_on))).all()}
        # 마감자 실명 — closed_by는 user id라 화면에 숫자만 뜨면 직무분리 사후확인이 무의미하다.
        # tellers_summary와 동일한 User→Employee 조인 규약.
        names: dict = {}
        actor_ids = {c.closed_by for c in rows if c.closed_by}
        if actor_ids:
            from erp.common.models import User
            from erp.hr.models import Employee
            names = {uid: nm for uid, nm in (await session.execute(
                select(User.id, Employee.name)
                .join(Employee, Employee.id == User.employee_id, isouter=True)
                .where(User.id.in_(actor_ids)))).all()}
        return [{"id": c.id, "closing_on": c.closing_on.isoformat(), "seq": c.seq,
                 "total_amount": float(c.total_amount), "payment_count": c.payment_count,
                 "refund_total": refunds.get(c.closing_on, (0.0, 0))[0],
                 "refund_count": refunds.get(c.closing_on, (0.0, 0))[1],
                 "status": c.status, "voucher_ref": c.voucher_ref,
                 "closed_by": c.closed_by,
                 "closed_by_name": (names.get(c.closed_by)
                                    or (f"user:{c.closed_by}" if c.closed_by else None)),
                 "closed_at": kst_iso(c.closed_at)} for c in rows]

    async def _aggregate(self, session: AsyncSession, on: date) -> tuple[float, int]:
        row = (await session.execute(
            select(func.coalesce(func.sum(Payment.amount), 0), func.count())
            .where(Payment.paid_on == on, Payment.status == "PAID")
        )).one()
        return float(row[0]), int(row[1])

    async def _next_seq(self, session: AsyncSession, on: date) -> int:
        return ((await session.execute(
            select(func.coalesce(func.max(DailyClosing.seq), 0))
            .where(DailyClosing.closing_on == on))).scalar_one()) + 1


class RefundService:
    """환불(과오납 반환) — 수납과 분리된 1급 거래 + 즉시 역전표(현금 반환)."""

    def __init__(self, vouchers: VoucherService | None = None):
        self._vouchers = vouchers or VoucherService()

    async def refund(self, session: AsyncSession, *, chart_no: str, amount: float, reason: str,
                     refunded_on: date, method: str = "CASH", reception_id: int | None = None,
                     actor: int | None = None) -> Refund:
        if amount <= 0:
            raise ValidationError("환불액은 0보다 커야 합니다")
        if not reason or not reason.strip():
            raise ValidationError("환불 사유는 필수입니다")
        amt = round(amount)
        reason = reason.strip()
        # 마감 게이트 — pay와 대칭(FR-54). 마감된 일자로 소급 환불 전기를 허용하면 그날
        # 마감보고·전표가 재집계되지 않아 원장이 오염됨. 환불은 실제 반환일(미마감)로 전기.
        await _lock_closing_day(session, refunded_on)
        if await PaymentService()._is_closed(session, refunded_on):
            raise ValidationError(
                "마감된 일자입니다 — 환불은 실제 반환일(미마감 일자)로 전기하세요",
                {"date": refunded_on.isoformat()})
        # 환자·접수 소속 검증 — 임의/미존재 chart로 가공 환불전표 전기 차단
        patient = await session.scalar(select(Patient).where(Patient.chart_no == chart_no))
        if patient is None:
            raise ValidationError("존재하지 않는 환자(차트번호)입니다")
        rc = None
        if reception_id is not None:
            rc = await session.scalar(select(Reception).where(Reception.id == reception_id))
            if rc is None or rc.patient_id != patient.id:
                raise ValidationError("접수건이 해당 환자에 속하지 않습니다")
        # 진료월 귀속 — 수납(care_month=접수월)과 대칭. 미연결 환불은 반환일 월 폴백(_prepaid 순차감 스코프).
        care_month = (rc.received_on.strftime("%Y-%m") if rc is not None
                      else refunded_on.strftime("%Y-%m"))
        # 멱등 — 더블클릭/재시도 방어(동일 chart·접수·금액·일자·사유 재요청은 기존 반환)
        dup = await session.scalar(select(Refund).where(
            Refund.chart_no == chart_no, Refund.reception_id == reception_id,
            Refund.amount == amt, Refund.refunded_on == refunded_on, Refund.reason == reason))
        if dup is not None:
            return dup
        # 과다환불 상한 — 순수납(PAID) − 기환불 누계 이내만 허용(낸 적 없는 금액 유출 차단)
        net_paid = float(await session.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0))
            .join(Reception, Reception.id == Payment.reception_id)
            .join(Patient, Patient.id == Reception.patient_id)
            .where(Patient.chart_no == chart_no, Payment.status == "PAID")) or 0)
        refunded = float(await session.scalar(
            select(func.coalesce(func.sum(Refund.amount), 0))
            .where(Refund.chart_no == chart_no)) or 0)
        available = net_paid - refunded
        if amt > available:
            raise ValidationError(
                f"환불액이 순수납 잔액({available:,.0f}원)을 초과합니다 (요청 {amt:,.0f}원)")
        from erp.config import get_settings
        ref = Refund(reception_id=reception_id, chart_no=chart_no, amount=amt,
                     method=method, reason=reason, refunded_on=refunded_on,
                     care_month=care_month, created_by=actor)
        session.add(ref)
        await session.flush()
        # 역전표 — 대변은 환불 수단별(수납 _revenue_lines와 대칭 — 계산검토 H-4): 카드취소→카드미수
        # (1132) 감소, 그 외(현금·계좌)→현금(1110). 종전 무조건 1110은 카드환불 시 현금시재 과소 왜곡.
        # 차변: 발생주의 ON이면 환자미수금(과오납 음수 상계), OFF면 외래수익 차감.
        debit = ACC_PATIENT_AR if get_settings().patient_accrual_enabled else ACC_REVENUE
        credit = ACC_CARD_AR if _is_card(method) else ACC_CASH
        v = await self._vouchers.create(
            session, source_module="patient", source_ref=f"refund-{ref.id}",
            voucher_on=refunded_on, created_by=actor, memo=f"환불 {chart_no} — {reason.strip()[:40]}",
            lines=[VoucherLineDTO(debit, "DEBIT", round(amount)),
                   VoucherLineDTO(credit, "CREDIT", round(amount))])
        ref.voucher_ref = f"voucher-{v.id}"
        return ref


class MedicalExpenseReportService:
    """의료비 연말정산 기초자료 (환자별 연간 수납액) — tax 모듈에서 이관(Phase0 결정 2026-07-23).

    환자 chart_no+실명을 다루므로 PHI-bound인 patient 모듈에 소속(tax는 location-free 유지).
    국세청 제출 파일은 주민번호 필요하나 ERP 미수집(D-6 정책) — 본 집계는 제출 전 기초자료.
    """
    async def annual_by_patient(self, session: AsyncSession, *, year: int) -> dict:
        from sqlalchemy import case
        # PAID만 합산 — 취소는 원행 CANCELED 플립 + 정정액 신규 PAID 추가 모델이라 CANCELED
        # 음수차감 시 이중차감(정정 환자 탈락). 코드베이스 공통 규약.
        paid = func.sum(case((Payment.status == "PAID", Payment.amount), else_=0))
        rows = (await session.execute(
            select(Patient.chart_no, Patient.name, paid)
            .select_from(Payment)
            .join(Reception, Reception.id == Payment.reception_id)
            .join(Patient, Patient.id == Reception.patient_id)
            .where(Payment.paid_on >= date(year, 1, 1),
                   Payment.paid_on <= date(year, 12, 31))
            .group_by(Patient.chart_no, Patient.name)
            .having(paid > 0)
            .order_by(paid.desc()))).all()
        items = [{"chart_no": c, "name": n, "total_paid": float(t)} for c, n, t in rows]
        return {"year": year, "count": len(items),
                "total": sum(i["total_paid"] for i in items), "items": items,
                "주": "주민등록번호 미수집(정책) — 국세청 제출 파일은 보안채널 확정 후 생성"}
