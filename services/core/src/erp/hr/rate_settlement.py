"""요율 차액 정산 (v1.295.0·0260) — 결정 대기함의 '지급분 차액 처리'를 선택과 동시에 실행한다.

2026-10-10: 2026 코드 요율이 2025 복사본이라 그 사이 확정한 급여의 근로자 공제가 과소(사업주분 동액).
결정 대기함에 선택지만 있고 실행 수단이 없으면 결정이 기록돼도 아무 일도 일어나지 않는다 — 방식 선택=실행+결정 기록.

방식
- NEXT_PAYROLL_ADJUST: 지정한 다음 급여월에 직원별 차액을 '소급정산' 공제 줄(NP_ADJ·HI_ADJ·LTC_ADJ·EI_ADJ)로 추가.
  급여 생성·재생성이 늘 다시 붙이고(upsert_lines), 확정 시 적용 완료로 표시. 대상 월 이전 퇴사자는 공제할 급여가
  없으므로 그 근로자분은 기관 부담(전표)으로 넘긴다.
- EMPLOYER_ABSORB: 직원 공제 없이 근로자분 전액을 기관 부담 전표(DR 5330 / CR 2110)로.
- NOT_PAYROLL_SOT: ERP 급여가 지급 원천이 아님 — 직원별 차액을 기록만 하고 급여·전표는 바꾸지 않는다.
사업주분(연금·건강·장기요양 = 근로자분과 같은 금액) 보정은 '사업주 부담 전기' 설정이 켜져 있을 때만 같은 전표에
포함한다(꺼져 있으면 원래 사업주분도 전기되지 않았으므로 보정할 것이 없다).
"""
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.months import require_ym
from erp.errors import ValidationError
from erp.hr.models import (
    Employee, PayrollLine, PayrollRecord, RateSettlement, RateSettlementItem,
)

METHODS = ("NEXT_PAYROLL_ADJUST", "EMPLOYER_ABSORB", "NOT_PAYROLL_SOT")
# 원 공제 코드 → (소급정산 공제 코드, 항목 컬럼)
ADJ_CODES = {"NP": ("NP_ADJ", "np"), "HI": ("HI_ADJ", "hi"), "LTC": ("LTC_ADJ", "ltc"), "EI": ("EI_ADJ", "ei")}
SETTLEMENT_LINE_CODES = tuple(c for c, _ in ADJ_CODES.values())
_EMPLOYER_SAME = ("NP", "HI", "LTC")          # 사업주 부담 = 근로자 부담과 같은 금액(고용보험은 요율이 달라 제외)
# 정산 결정 — 결정 대기함 key ↔ 원 급여월. 이 결정은 실행 화면에서만 기록된다(admin decide 차단).
DECISION_BY_SOURCE = {"2026-07": "statutory-2026-rates-check"}
DECISION_SCREEN = "/admin/payroll-settings"
_ACC_WELFARE, _ACC_PAYABLE = "5330", "2110"

_audit = AuditService()


def _month_first(ym: str):
    from datetime import date
    return date(int(ym[:4]), int(ym[5:7]), 1)


def _target_month_ended(ym: str | None) -> bool:
    """공제월이 끝났는가(KST) — 미적용 항목을 '확정 대기'와 '끝내 못 받음'으로 가르는 기준(absorb_remaining과 같음)."""
    if not ym:
        return False
    from erp.common.kst import kst_today
    tm = _month_first(ym)
    nxt = tm.replace(year=tm.year + 1, month=1) if tm.month == 12 else tm.replace(month=tm.month + 1)
    return kst_today() >= nxt


async def _active(session: AsyncSession, source: str, *, lock: bool = False) -> RateSettlement | None:
    q = select(RateSettlement).where(RateSettlement.source_yyyymm == source, RateSettlement.status == "ACTIVE")
    if lock:
        q = q.with_for_update()
    return await session.scalar(q)


async def _decision(session: AsyncSession, source: str, *, lock: bool = False):
    from erp.common.models import PendingDecision
    key = DECISION_BY_SOURCE.get(source)
    if key is None:
        return None
    q = select(PendingDecision).where(PendingDecision.key == key)
    if lock:
        q = q.with_for_update()
    return await session.scalar(q)


def _decision_dict(d) -> dict | None:
    if d is None:
        return None
    return {"id": d.id, "key": d.key, "title": d.title, "status": d.status,
            "decided_option": d.decided_option, "decided_at": d.decided_at,
            "options": [{"key": o.get("key"), "label": o.get("label"), "description": o.get("description"),
                         "recommended": bool(o.get("recommended"))} for o in (d.options or [])]}


async def _plan(session: AsyncSession, source: str, method: str, target: str | None) -> dict:
    """실행 전 계획(쓰기 없음) — 직원별 처분·전표 금액·막는 사유."""
    from erp.hr.statutory_params import diff_records
    if method not in METHODS:
        raise ValidationError(f"정산 방식은 {', '.join(METHODS)} 중 하나입니다")
    d = await diff_records(session, yyyymm=source)
    blockers: list[str] = []
    if method == "NEXT_PAYROLL_ADJUST":
        if not target:
            raise ValidationError("다음 급여 정산은 공제할 급여월(target_yyyymm)이 필요합니다")
        target = require_ym(target)
        if target <= source:
            raise ValidationError(f"공제 급여월({target})은 원 급여월({source}) 이후여야 합니다")
    else:
        target = None
    rows = d["rows"]
    # 원 급여월 미확정분 — 재생성하면 새 요율로 계산되므로 정산하면 이중 정정이 된다(검토 MED). 정산은 확정분만,
    # 미확정분이 남아 있으면 실행을 막는다(재생성 후 확정하거나 확정 후 실행 — 확정된 급여는 다시 생성되지 않는다).
    unconfirmed = [r["employee_no"] for r in rows if not r["confirmed"]]
    if unconfirmed:
        blockers.append(f"{source} 급여 중 미확정 {len(unconfirmed)}건({', '.join(unconfirmed[:10])}"
                        f"{' 외' if len(unconfirmed) > 10 else ''}) — 미확정 급여는 재생성하면 새 요율이 적용됩니다. "
                        "재생성·확정을 먼저 마친 뒤 실행하세요")
    if target:
        # 같은 공제월을 노리는 다른 활성 정산 — 직원당 공제 줄이 한 정산분만 붙어 나머지가 조용히 사라진다(검토 HIGH)
        other = await session.scalar(select(RateSettlement.source_yyyymm).where(
            RateSettlement.status == "ACTIVE", RateSettlement.method == "NEXT_PAYROLL_ADJUST",
            RateSettlement.target_yyyymm == target, RateSettlement.source_yyyymm != source))
        if other:
            blockers.append(f"{other} 정산이 이미 {target} 급여에 공제를 붙이고 있습니다 — 다른 공제월을 고르세요")
    confirmed_target: set[int] = set()
    if target and rows:
        confirmed_target = set((await session.scalars(select(PayrollRecord.employee_id).where(
            PayrollRecord.yyyymm == target, PayrollRecord.confirmed_at.isnot(None),
            PayrollRecord.employee_id.in_([r["employee_id"] for r in rows])))).all())
    # 사업주분 보정 — 오늘 설정이 아니라 원 급여월에 사업주분 전표가 **실제로 전기됐는지**를 직원별로(검토 MED)
    posted = await _employer_posted_records(session, [r["record_id"] for r in rows])
    items, employee_total, employer_total, employer_correction, absorbed = [], 0, 0, 0, 0
    late: list[str] = []
    for r in rows:
        diff = r["diff"]
        total = sum(diff.values())
        employee_total += total
        er = sum(v for c, v in diff.items() if c in _EMPLOYER_SAME)
        employer_total += er
        if r["record_id"] in posted:
            employer_correction += er
        if method == "NOT_PAYROLL_SOT":
            disp = "RECORD"
        elif method == "EMPLOYER_ABSORB":
            disp = "ABSORB"
        elif r["resigned_on"] is not None and r["resigned_on"] < _month_first(target):
            disp = "ABSORB"                          # 공제할 급여가 없다(대상 월 전 퇴사) — 기관 부담
        else:
            disp = "DEDUCT"
            if r["employee_id"] in confirmed_target:
                late.append(r["employee_no"])
        if disp == "ABSORB":
            absorbed += total
        items.append({**r, "disposition": disp})
    if late:
        blockers.append(f"{target} 급여가 이미 확정된 직원 {len(late)}명({', '.join(late[:10])}"
                        f"{' 외' if len(late) > 10 else ''}) — 확정 급여엔 공제를 붙일 수 없습니다. 더 뒤의 급여월을 고르세요")
    if method == "NOT_PAYROLL_SOT":
        employer_correction = 0
    voucher = 0 if method == "NOT_PAYROLL_SOT" else absorbed + employer_correction
    return {"source_yyyymm": source, "method": method, "target_yyyymm": target,
            "records": d["records"], "affected": len(rows), "totals": d["totals"],
            "employee_total": employee_total, "employer_total": employer_total,
            "employer_correction": employer_correction, "employer_posted_records": len(posted),
            "absorbed_total": absorbed, "voucher_amount": voucher,
            "deduct_count": sum(1 for i in items if i["disposition"] == "DEDUCT"),
            "absorb_count": sum(1 for i in items if i["disposition"] == "ABSORB"),
            "blocked": " · ".join(blockers) if blockers else None, "items": items}


async def _employer_posted_records(session: AsyncSession, record_ids: list[int]) -> set[int]:
    """사업주 부담금 전표(payroll-employer:{record_id})가 전기된 급여 — 보정 대상 판정의 근거."""
    if not record_ids:
        return set()
    from erp.finance.models import Voucher
    refs = {f"payroll-employer:{i}": i for i in record_ids}
    got = (await session.scalars(select(Voucher.source_ref).where(
        Voucher.source_module == "hr.payroll", Voucher.source_ref.in_(list(refs)),
        Voucher.status != "REVERSED"))).all()
    return {refs[r] for r in got}


async def overview(session: AsyncSession, *, source: str) -> dict:
    """화면 상단 — 원 급여월의 정산 현황(활성 정산·이력·연결된 결정)."""
    source = require_ym(source)
    rows = (await session.scalars(select(RateSettlement).where(RateSettlement.source_yyyymm == source)
                                  .order_by(RateSettlement.id.desc()))).all()
    out = []
    for s in rows:
        items = (await session.scalars(select(RateSettlementItem).where(
            RateSettlementItem.settlement_id == s.id))).all()
        pending = [i.employee_id for i in items if i.disposition == "DEDUCT" and i.applied_record_id is None]
        out.append({**_settlement_dict(s),
                    "items": len(items),
                    "deduct": sum(1 for i in items if i.disposition == "DEDUCT"),
                    "applied": sum(1 for i in items if i.applied_record_id is not None),
                    "pending": len(pending), "target_month_ended": _target_month_ended(s.target_yyyymm)})
    return {"source_yyyymm": source, "settlements": out,
            "decision": _decision_dict(await _decision(session, source))}


def _settlement_dict(s: RateSettlement) -> dict:
    return {"id": s.id, "source_yyyymm": s.source_yyyymm, "method": s.method, "target_yyyymm": s.target_yyyymm,
            "status": s.status, "note": s.note, "employee_total": int(s.employee_total or 0),
            "employer_total": int(s.employer_total or 0), "absorbed_total": int(s.absorbed_total or 0),
            "voucher_ref": s.voucher_ref, "decision_key": s.decision_key,
            "created_by": s.created_by, "created_at": s.created_at,
            "canceled_at": s.canceled_at, "cancel_reason": s.cancel_reason}


async def preview(session: AsyncSession, *, source: str, method: str, target: str | None) -> dict:
    source = require_ym(source)
    p = await _plan(session, source, method, target)
    p["items"] = [{k: i[k] for k in ("employee_no", "diff", "total", "disposition", "resigned_on")}
                  for i in p["items"][:500]]
    p["active"] = (_settlement_dict(a) if (a := await _active(session, source)) else None)
    return p


async def execute(session: AsyncSession, *, actor: int, source: str, method: str,
                  target: str | None, note: str) -> dict:
    """방식 실행 + 결정 기록(원자적). 같은 급여월에 활성 정산이 있으면 거부(취소 후 다시)."""
    source = require_ym(source)
    note = (note or "").strip()
    if len(note) < 4:
        raise ValidationError("실행 사유(근거)를 4자 이상 입력하세요 — 결정 기록에 남습니다")
    if await _active(session, source, lock=True):
        raise ValidationError(f"{source} 정산이 이미 실행돼 있습니다 — 바꾸려면 먼저 취소하세요")
    dec = await _decision(session, source, lock=True)
    if dec is not None and dec.status == "DECIDED":
        raise ValidationError("연결된 결정이 이미 기록돼 있습니다(정산 없이) — 결정·조치함에서 재오픈 후 실행하세요")
    if dec is not None and method not in {o.get("key") for o in (dec.options or [])}:
        raise ValidationError("결정 선택지에 없는 방식입니다", {"method": method})
    if method == "NEXT_PAYROLL_ADJUST" and target:
        # 같은 공제월 중복 검사의 경합 — 서로 다른 원 급여월 두 실행이 동시에 통과하지 않게 공제월 단위 직렬화(PG)
        from erp.common.doc_no import lock_doc_no
        await lock_doc_no(session, f"rate-settlement-target:{require_ym(target)}")
    p = await _plan(session, source, method, target)
    if p["blocked"]:
        raise ValidationError(p["blocked"])
    if not p["items"]:
        raise ValidationError(f"{source} 급여에 요율 차액이 없습니다 — 정산할 것이 없습니다")
    from erp.hr import statutory
    s = RateSettlement(source_yyyymm=source, method=method, target_yyyymm=p["target_yyyymm"], status="ACTIVE",
                       decision_key=dec.key if dec is not None else None, note=note[:1000],
                       employee_total=p["employee_total"], employer_total=p["employer_total"],
                       absorbed_total=p["absorbed_total"], rates=statutory.insurance_rates(int(source[:4])),
                       created_by=actor)
    from sqlalchemy.exc import IntegrityError
    try:
        async with session.begin_nested():
            session.add(s)
            await session.flush()
    except IntegrityError:                      # 동시 실행 경합 패자(부분 유니크 uq_rate_settle_active)
        raise ValidationError(f"{source} 정산이 동시에 실행됐습니다 — 화면을 새로 고쳐 실행 결과를 확인하세요") from None
    for i in p["items"]:
        session.add(RateSettlementItem(
            settlement_id=s.id, employee_id=i["employee_id"], source_record_id=i["record_id"],
            disposition=i["disposition"],
            **{col: i["diff"].get(code, 0) for code, (_adj, col) in ADJ_CODES.items()}))
    await session.flush()
    if p["voucher_amount"]:
        s.voucher_ref = await _post_voucher(session, s, p, actor)
    if method == "NEXT_PAYROLL_ADJUST":     # 이미 생성(미확정)된 대상 월 급여엔 지금 붙인다
        for rec in (await session.scalars(select(PayrollRecord).where(
                PayrollRecord.yyyymm == s.target_yyyymm, PayrollRecord.confirmed_at.is_(None)))).all():
            await attach_lines(session, rec)
    if dec is not None:
        dec.status, dec.decided_option = "DECIDED", method
        dec.decided_note = f"[요율 차액 정산 #{s.id} 실행] {note}"[:1000]
        dec.decided_by, dec.decided_at = actor, datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="DECIDE", entity="common.pending_decisions",
                                  target_id=dec.key, after={"option": method, "note": dec.decided_note,
                                                            "settlement_id": s.id})
    await _audit.record_audit(session, actor=actor, action="EXECUTE", entity="hr.rate_settlements",
                              target_id=str(s.id), after={**_settlement_dict(s), "created_at": None,
                                                          "deduct": p["deduct_count"], "absorb": p["absorb_count"],
                                                          "voucher_amount": p["voucher_amount"]})
    await session.flush()
    return {**_settlement_dict(s), "deduct": p["deduct_count"], "absorb": p["absorb_count"],
            "voucher_amount": p["voucher_amount"]}


async def _post_voucher(session: AsyncSession, s: RateSettlement, p: dict, actor: int) -> str:
    from erp.common.kst import kst_today
    from erp.finance.service import VoucherLineDTO, VoucherService
    amt = int(p["voucher_amount"])
    parts = []
    if p["absorbed_total"]:
        parts.append(f"근로자분 기관부담 {int(p['absorbed_total']):,}")
    if p["employer_correction"]:
        parts.append(f"사업주분 보정 {int(p['employer_correction']):,}")
    dr, cr = (_ACC_WELFARE, _ACC_PAYABLE) if amt > 0 else (_ACC_PAYABLE, _ACC_WELFARE)
    ref = f"rate-settlement:{s.id}"
    await VoucherService().create(
        session, source_module="hr.payroll", source_ref=ref, voucher_on=kst_today(), created_by=actor,
        memo=f"4대보험 요율 차액 정산 {s.source_yyyymm} — {' · '.join(parts)}"[:200],
        lines=[VoucherLineDTO(dr, "DEBIT", abs(amt)), VoucherLineDTO(cr, "CREDIT", abs(amt))])
    return ref


async def attach_lines(session: AsyncSession, record: PayrollRecord) -> int:
    """급여(미확정)에 활성 '다음 급여 정산' 공제 줄을 붙인다 — 생성·재생성(upsert_lines)·실행 시 호출. 멱등."""
    if record.confirmed_at is not None:
        return 0
    item = await session.scalar(
        select(RateSettlementItem).join(RateSettlement, RateSettlement.id == RateSettlementItem.settlement_id)
        .where(RateSettlement.status == "ACTIVE", RateSettlement.method == "NEXT_PAYROLL_ADJUST",
               RateSettlement.target_yyyymm == record.yyyymm, RateSettlementItem.employee_id == record.employee_id,
               RateSettlementItem.disposition == "DEDUCT"))
    await session.execute(delete(PayrollLine).where(PayrollLine.record_id == record.id,
                                                    PayrollLine.item_code.in_(SETTLEMENT_LINE_CODES)))
    if item is None:
        return 0
    s = await session.get(RateSettlement, item.settlement_id)
    n = 0
    for code, (adj, col) in ADJ_CODES.items():
        amt = int(getattr(item, col) or 0)
        if amt:
            session.add(PayrollLine(record_id=record.id, item_code=adj, item_kind="DEDUCTION", amount=amt,
                                    calc_basis={"formula": f"{s.source_yyyymm} {code} 요율 정정 소급분",
                                                "settlement_id": s.id, "source_yyyymm": s.source_yyyymm}))
            n += 1
    return n


async def mark_applied(session: AsyncSession, record: PayrollRecord) -> None:
    """급여 확정 시 — 이 급여에 붙은 정산 항목을 적용 완료로(취소 차단 근거)."""
    items = (await session.scalars(
        select(RateSettlementItem).join(RateSettlement, RateSettlement.id == RateSettlementItem.settlement_id)
        .where(RateSettlement.status == "ACTIVE", RateSettlement.method == "NEXT_PAYROLL_ADJUST",
               RateSettlement.target_yyyymm == record.yyyymm, RateSettlementItem.employee_id == record.employee_id,
               RateSettlementItem.disposition == "DEDUCT", RateSettlementItem.applied_record_id.is_(None)))).all()
    now = datetime.now(timezone.utc)
    for i in items:
        i.applied_record_id, i.applied_at = record.id, now


async def cancel(session: AsyncSession, *, actor: int, settlement_id: int, reason: str) -> dict:
    """정산 취소 — 공제가 확정된 급여가 하나라도 있으면 거부. 전표 역분개·미확정 급여의 공제 줄 제거·결정 재오픈."""
    reason = (reason or "").strip()
    if len(reason) < 4:
        raise ValidationError("취소 사유를 4자 이상 입력하세요")
    s = await session.scalar(select(RateSettlement).where(RateSettlement.id == settlement_id).with_for_update())
    if s is None or s.status != "ACTIVE":
        raise ValidationError("취소할 수 있는 정산이 아닙니다")
    applied = (await session.scalars(select(Employee.employee_no).join(
        RateSettlementItem, RateSettlementItem.employee_id == Employee.id).where(
        RateSettlementItem.settlement_id == s.id, RateSettlementItem.applied_record_id.isnot(None)))).all()
    if applied:
        raise ValidationError(f"{s.target_yyyymm} 급여에 공제가 확정된 직원 {len(applied)}명이 있어 취소할 수 없습니다 — "
                              "확정 급여의 정정 절차(전표 역분개 후 다음 달 반영)로 처리하세요")
    from erp.finance.models import Voucher
    from erp.finance.service import VoucherService
    for v in (await session.scalars(select(Voucher).where(   # 실행 전표 + 남은 공제분 기관 부담 전표
            Voucher.source_module == "hr.payroll",
            (Voucher.source_ref == f"rate-settlement:{s.id}") | Voucher.source_ref.like(f"rate-settlement:{s.id}:%"),
            Voucher.status != "REVERSED", Voucher.reverses.is_(None)))).all():   # 역분개 전표는 되돌리지 않는다
        await VoucherService().reverse(session, voucher_id=v.id, reason=f"요율 차액 정산 #{s.id} 취소: {reason}"[:200],
                                       created_by=actor)
    s.status, s.canceled_by, s.canceled_at = "CANCELED", actor, datetime.now(timezone.utc)
    s.cancel_reason = reason[:500]
    await session.flush()
    if s.target_yyyymm:
        for rec in (await session.scalars(select(PayrollRecord).where(
                PayrollRecord.yyyymm == s.target_yyyymm, PayrollRecord.confirmed_at.is_(None)))).all():
            await attach_lines(session, rec)
    dec = await _decision(session, s.source_yyyymm, lock=True)
    if dec is not None and dec.status == "DECIDED" and s.decision_key == dec.key:
        before = {"option": dec.decided_option, "note": dec.decided_note}
        dec.status, dec.decided_option, dec.decided_note = "PENDING", None, None
        dec.decided_by = dec.decided_at = None
        await _audit.record_audit(session, actor=actor, action="REOPEN", entity="common.pending_decisions",
                                  target_id=dec.key, before=before,
                                  after={"note": f"[요율 차액 정산 #{s.id} 취소] {reason}"[:1000]})
    await _audit.record_audit(session, actor=actor, action="CANCEL", entity="hr.rate_settlements",
                              target_id=str(s.id), after={"reason": s.cancel_reason})
    await session.flush()
    return _settlement_dict(s)


async def absorb_remaining(session: AsyncSession, *, actor: int, settlement_id: int, reason: str) -> dict:
    """'다음 급여 정산' 중 끝내 공제되지 못한 직원분을 기관 부담으로 돌린다(검토 MED — 출구 없는 미회수 방지).

    대상: 공제 미적용 DEDUCT 항목 중 공제월 미확정 급여가 **없는** 직원(실행 후 퇴사·휴직·미생성 등). 공제월 급여 확정이
    한 건 이상 끝난 뒤에만(아직 생성 전인 직원을 성급히 넘기지 않게). 보충 전표 rate-settlement:{id}:remain:{n}.
    """
    reason = (reason or "").strip()
    if len(reason) < 4:
        raise ValidationError("처리 사유를 4자 이상 입력하세요")
    s = await session.scalar(select(RateSettlement).where(RateSettlement.id == settlement_id).with_for_update())
    if s is None or s.status != "ACTIVE" or s.method != "NEXT_PAYROLL_ADJUST":
        raise ValidationError("'다음 급여 정산'으로 실행 중인 정산만 처리할 수 있습니다")
    if not _target_month_ended(s.target_yyyymm) or not await session.scalar(select(PayrollRecord.id).where(
            PayrollRecord.yyyymm == s.target_yyyymm, PayrollRecord.confirmed_at.isnot(None)).limit(1)):
        raise ValidationError(f"{s.target_yyyymm} 급여 확정이 시작되고 그 달이 끝난 뒤에 처리하세요 — "
                              "아직 생성 전인 직원(일용직 등)이 있을 수 있습니다")
    open_recs = set((await session.scalars(select(PayrollRecord.employee_id).where(
        PayrollRecord.yyyymm == s.target_yyyymm, PayrollRecord.confirmed_at.is_(None)))).all())
    items = [i for i in (await session.scalars(select(RateSettlementItem).where(
        RateSettlementItem.settlement_id == s.id, RateSettlementItem.disposition == "DEDUCT",
        RateSettlementItem.applied_record_id.is_(None)))).all() if i.employee_id not in open_recs]
    if not items:
        raise ValidationError("기관 부담으로 돌릴 미적용 공제분이 없습니다(공제월 미확정 급여가 남은 직원은 확정을 기다립니다)")
    amt = 0
    for i in items:
        amt += sum(int(getattr(i, col) or 0) for _c, (_a, col) in ADJ_CODES.items())
        i.disposition = "ABSORB"
    s.absorbed_total = int(s.absorbed_total or 0) + amt
    from erp.common.kst import kst_today
    from erp.finance.models import Voucher
    from erp.finance.service import VoucherLineDTO, VoucherService
    n = await session.scalar(select(func.count()).select_from(Voucher).where(
        Voucher.source_ref.like(f"rate-settlement:{s.id}:remain:%")))
    ref = f"rate-settlement:{s.id}:remain:{(n or 0) + 1}"
    if amt:
        dr, cr = (_ACC_WELFARE, _ACC_PAYABLE) if amt > 0 else (_ACC_PAYABLE, _ACC_WELFARE)
        await VoucherService().create(
            session, source_module="hr.payroll", source_ref=ref, voucher_on=kst_today(), created_by=actor,
            memo=f"4대보험 요율 차액 정산 {s.source_yyyymm} — 미공제분 기관부담 {len(items)}명 {amt:,}"[:200],
            lines=[VoucherLineDTO(dr, "DEBIT", abs(amt)), VoucherLineDTO(cr, "CREDIT", abs(amt))])
    await _audit.record_audit(session, actor=actor, action="ABSORB_REMAINING", entity="hr.rate_settlements",
                              target_id=str(s.id), after={"employees": len(items), "amount": amt, "voucher": ref,
                                                          "reason": reason[:500]})
    await session.flush()
    return {**_settlement_dict(s), "absorbed_now": amt, "employees": len(items), "voucher": ref if amt else None}
