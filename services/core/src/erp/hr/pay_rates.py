"""수당 단가 로더/관리 (P2-②·거버넌스 기간화) — hr.pay_rate_settings → build_payroll 주입.

(rate_key, valid_from) 기간 이력: 연 단위 단협·취업규칙 갱신 시 새 발효행을 추가(기존 불변 보존 =
변경 로그). 급여 생성은 급여월 기준(as_of) 유효 단가를 사용 — 단가 개정이 과거 급여 재생성을
소급 왜곡하지 않는다. 미설정 키는 payroll_rules 코드 기본값(하위호환).
"""
from datetime import date

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import PayRateSetting
from erp.hr.payroll_rules import DEFAULT_RATES
from erp.common.kst import kst_iso

_audit = AuditService()


def _effective(rows: list[PayRateSetting], as_of: date) -> dict[str, PayRateSetting]:
    """rate_key별 as_of 시점 유효 행(발효일 ≤ as_of 중 최신)."""
    eff: dict[str, PayRateSetting] = {}
    for r in sorted(rows, key=lambda x: x.valid_from):
        if r.valid_from <= as_of:
            eff[r.rate_key] = r
    return eff


async def load_rates(session: AsyncSession, as_of: date | None = None) -> dict:
    """as_of(기본 오늘) 시점 유효 단가 로드 — 급여 생성은 급여월 1일을 전달(소급 정확성)."""
    rows = (await session.scalars(select(PayRateSetting))).all()
    return {k: float(r.amount) for k, r in _effective(rows, as_of or date.today()).items()}


async def list_rates(session: AsyncSession, as_of: date | None = None) -> list[dict]:
    asof = as_of or date.today()
    rows = (await session.scalars(select(PayRateSetting))).all()
    eff = _effective(rows, asof)
    history_n: dict[str, int] = {}
    # 미래 발효 예약행(valid_from > as_of 중 최근접) — 목록에 표시하지 않으면 "미등록"으로 오인해
    # 중복 등록 유발(적대검토 LOW). 키별 다음 예약 인상분을 노출.
    future: dict[str, PayRateSetting] = {}
    for r in rows:
        history_n[r.rate_key] = history_n.get(r.rate_key, 0) + 1
        if r.valid_from > asof and (r.rate_key not in future
                                    or r.valid_from < future[r.rate_key].valid_from):
            future[r.rate_key] = r

    def _future(key: str) -> dict | None:
        f = future.get(key)
        return None if f is None else {"valid_from": f.valid_from.isoformat(),
                                       "amount": float(f.amount)}

    out = []
    for key, default in DEFAULT_RATES.items():
        row = eff.get(key)
        out.append({"rate_key": key, "amount": float(row.amount) if row else default,
                    "default": default, "overridden": row is not None,
                    "valid_from": row.valid_from.isoformat() if row else None,
                    "history_count": history_n.get(key, 0),
                    "description": row.description if row else None,
                    "next": _future(key)})
    # 코드 기본값에 없는 운영 추가 키도 노출(현행 유효 없이 미래 예약만 있는 키 포함)
    seen = set(DEFAULT_RATES) | set(eff)
    out += [{"rate_key": k, "amount": float(r.amount), "default": None,
             "overridden": True, "valid_from": r.valid_from.isoformat(),
             "history_count": history_n.get(k, 0), "description": r.description,
             "next": _future(k)}
            for k, r in eff.items() if k not in DEFAULT_RATES]
    out += [{"rate_key": k, "amount": None, "default": None, "overridden": False,
             "valid_from": None, "history_count": history_n.get(k, 0),
             "description": None, "next": _future(k)}
            for k in future if k not in seen]
    return out


async def upsert_rate(session: AsyncSession, *, actor: int, rate_key: str,
                      amount: float, description: str | None = None,
                      valid_from: date | None = None) -> PayRateSetting:
    """단가 설정 — 발효일(기본 오늘) 기준 **새 기간 행 추가**(동일 발효일이면 그 행만 갱신).

    과거 발효행은 불변 보존(이력 = 변경 로그) — 연 단위 갱신은 새 valid_from으로 등록.
    """
    if amount < 0:
        raise ValidationError("단가는 음수일 수 없습니다")
    vf = valid_from or date.today()
    row = await session.scalar(select(PayRateSetting).where(
        PayRateSetting.rate_key == rate_key, PayRateSetting.valid_from == vf))
    before = {"amount": float(row.amount), "valid_from": vf.isoformat()} if row else None
    if row is None:
        # SELECT-then-INSERT 경합 — 유니크(rate_key, valid_from) 충돌 시 500 대신 재조회해 갱신
        # (동시 저장·적대검토 LOW). 세이브포인트로 부분 롤백(외부 트랜잭션 보존).
        try:
            async with session.begin_nested():
                row = PayRateSetting(rate_key=rate_key, amount=amount, description=description,
                                     valid_from=vf, updated_by=actor)
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = await session.scalar(select(PayRateSetting).where(
                PayRateSetting.rate_key == rate_key, PayRateSetting.valid_from == vf))
            before = {"amount": float(row.amount), "valid_from": vf.isoformat()}
            row.amount = amount
            if description is not None:
                row.description = description
            row.updated_by = actor
    else:
        row.amount = amount
        if description is not None:
            row.description = description
        row.updated_by = actor
    await _audit.record_audit(session, actor=actor,
                              action="UPDATE" if before else "CREATE",
                              entity="hr.pay_rate_settings", target_id=rate_key,
                              before=before,
                              after={"amount": amount, "valid_from": vf.isoformat()})
    return row


async def delete_rate(session: AsyncSession, *, actor: int, rate_key: str,
                      valid_from: date) -> dict:
    """단가 발효행 삭제 — 오등록(발효일 오타·금액 오등록) 정정용.

    upsert는 같은 (rate_key, valid_from)만 덮어쓸 뿐, 잘못 만든 과거 발효행 자체는 제거 못 해
    이후 급여 재생성이 그 오행을 유효 단가로 채택해 소급 왜곡(적대검토 MED). 삭제로 복구 경로 제공.
    감사 기록 남김(거버넌스 로그).
    """
    row = await session.scalar(select(PayRateSetting).where(
        PayRateSetting.rate_key == rate_key, PayRateSetting.valid_from == valid_from))
    if row is None:
        raise ValidationError("해당 발효일의 단가 행이 없습니다",
                              {"rate_key": rate_key, "valid_from": valid_from.isoformat()})
    before = {"amount": float(row.amount), "valid_from": valid_from.isoformat(),
              "description": row.description}
    await session.delete(row)
    await _audit.record_audit(session, actor=actor, action="DELETE",
                              entity="hr.pay_rate_settings", target_id=rate_key,
                              before=before, after=None)
    await session.flush()
    return {"deleted": True, "rate_key": rate_key, "valid_from": valid_from.isoformat()}


async def rate_history(session: AsyncSession, *, rate_key: str) -> list[dict]:
    """단가 변경 이력(발효일 내림차순) — 거버넌스 로그 조회."""
    rows = (await session.scalars(
        select(PayRateSetting).where(PayRateSetting.rate_key == rate_key)
        .order_by(PayRateSetting.valid_from.desc()))).all()
    return [{"valid_from": r.valid_from.isoformat(), "amount": float(r.amount),
             "description": r.description, "updated_by": r.updated_by,
             "updated_at": kst_iso(r.updated_at)}
            for r in rows]
