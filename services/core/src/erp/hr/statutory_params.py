"""법정 파라미터 등록(관리화면) — 4대보험 요율·소득세 전년 승계. 검증·감사·캐시 갱신."""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr import statutory
from erp.hr.models import StatutoryParam

# 근로자 부담 요율의 상식 범위 — 소수/퍼센트 혼동(4.75를 0.0475로 입력해야 하는데 4.75 입력)과
# 자릿수 실수를 막는다. 고시값 자체를 판정하지 않는다(범위 안이면 사람의 입력을 존중).
_BOUNDS = {"pension": (0.03, 0.08), "health": (0.02, 0.06), "longterm_of_health": (0.05, 0.3),
           "employment": (0.005, 0.02)}

_audit = AuditService()


def _validate_insurance(values: dict) -> dict:
    missing = [k for k in statutory.INSURANCE_KEYS if values.get(k) is None]
    if missing:
        raise ValidationError(f"요율 항목 누락: {', '.join(missing)}")
    out: dict = {}
    for k in statutory.INSURANCE_KEYS:
        try:
            v = float(values[k])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{k} 값이 숫자가 아닙니다") from exc
        if k in _BOUNDS:
            lo, hi = _BOUNDS[k]
            if not lo <= v <= hi:
                raise ValidationError(f"{k}={v} — 범위 {lo}~{hi} 밖입니다(근로자 부담 소수 요율로 입력·"
                                      "예: 4.75% → 0.0475)")
        out[k] = v
    out["pension_min"], out["pension_max"] = int(out["pension_min"]), int(out["pension_max"])
    if not 100_000 <= out["pension_min"] < out["pension_max"] <= 30_000_000:
        raise ValidationError("국민연금 기준소득월액 하한·상한이 올바르지 않습니다 — 원 단위로 입력"
                              "(예: 하한 400,000 · 상한 6,370,000)")
    # 선택: 7월분부터의 상·하한(공단은 매년 7월 조정) — 둘 다 넣거나 둘 다 비운다
    h2 = [values.get(k) not in (None, "", 0) for k in statutory.INSURANCE_OPTIONAL_KEYS]
    if any(h2) and not all(h2):
        raise ValidationError("7월분부터의 국민연금 하한·상한은 둘 다 입력하거나 둘 다 비우세요")
    if all(h2):
        try:
            lo, hi = (int(float(values[k])) for k in statutory.INSURANCE_OPTIONAL_KEYS)
        except (TypeError, ValueError) as exc:
            raise ValidationError("7월분부터의 국민연금 하한·상한이 숫자가 아닙니다") from exc
        if not 100_000 <= lo < hi <= 30_000_000:
            raise ValidationError("7월분부터의 국민연금 하한·상한이 올바르지 않습니다 — 원 단위로 입력"
                                  "(예: 하한 410,000 · 상한 6,590,000)")
        out["pension_min_h2"], out["pension_max_h2"] = lo, hi
    # 선택: 사업주 고용보험·산재 요율(각각 독립·소수 요율) — 고용보험 사업주 0.8~3%, 산재 업종별 0.5~25%
    mw = values.get("min_wage_hourly")
    if mw not in (None, "", 0):
        try:
            v = int(float(mw))
        except (TypeError, ValueError) as exc:
            raise ValidationError("최저임금 시급이 숫자가 아닙니다") from exc
        if not 5_000 <= v <= 50_000:
            raise ValidationError(f"최저임금 시급 {v}원 — 원 단위 시급으로 입력하세요(예: 10,700)")
        out["min_wage_hourly"] = v
    for k, (lo, hi) in (("employer_ei", (0.008, 0.03)), ("employer_wc", (0.005, 0.25))):
        if values.get(k) in (None, "", 0):
            continue
        try:
            v = float(values[k])
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{k} 값이 숫자가 아닙니다") from exc
        if not lo <= v <= hi:
            raise ValidationError(f"{k}={v} — 범위 {lo}~{hi} 밖입니다(소수 요율로 입력·예: 1.15% → 0.0115)")
        out[k] = v
    return out


async def _upsert(session: AsyncSession, *, year: int, kind: str, values: dict, source_note: str,
                  actor: int) -> StatutoryParam:
    note = (source_note or "").strip()
    if len(note) < 4:
        raise ValidationError("근거(고시 번호·출처)를 입력하세요")
    if not 2020 <= year <= 2100:
        raise ValidationError("연도가 올바르지 않습니다")
    row = await session.scalar(select(StatutoryParam).where(
        StatutoryParam.year == year, StatutoryParam.kind == kind))
    before = dict(row.values) if row is not None else None
    if row is None:
        row = StatutoryParam(year=year, kind=kind, values=values, source_note=note[:500],
                             updated_by=actor)
        session.add(row)
    else:
        row.values, row.source_note, row.updated_by = values, note[:500], actor
    await session.flush()
    await _audit.record_audit(session, actor=actor, action="STATUTORY_PARAM_UPSERT",
                              entity="hr.statutory_params", target_id=f"{year}:{kind}",
                              before=before, after={**values, "source_note": note[:200]})
    return row


async def set_insurance(session: AsyncSession, *, year: int, values: dict, source_note: str,
                        actor: int) -> StatutoryParam:
    return await _upsert(session, year=year, kind="INSURANCE", values=_validate_insurance(values),
                         source_note=source_note, actor=actor)


async def carry_tax(session: AsyncSession, *, year: int, from_year: int, source_note: str,
                    actor: int) -> StatutoryParam:
    """세법 개정 없음 확인 → 전년(등록된 연도) 소득세 파라미터 승계."""
    if from_year >= year:
        raise ValidationError("승계 원본은 대상 연도보다 이전이어야 합니다")
    statutory._tax_params(from_year)            # 원본이 실제로 등록돼 있어야 한다(아니면 예외)
    if year in statutory.TAX_PARAMS:
        raise ValidationError(f"{year}년 소득세 파라미터는 코드에 이미 등록돼 있습니다")
    await _drop_other_tax_kind(session, year=year, keep="TAX_CARRY")
    return await _upsert(session, year=year, kind="TAX_CARRY", values={"from_year": int(from_year)},
                         source_note=source_note, actor=actor)


# 율(0~1) — 스칼라 키와 구간표의 율 칸(열 번호). '6%를 6으로' 같은 자릿수 실수를 막는다(daily_credit 55 → 일용 세액 0 등).
_RATE_KEYS = {"eitc_rate_low", "eitc_rate_high", "daily_rate", "daily_credit"}
_TABLE_RATE_COL = {"brackets": 1, "earned_deduction": 2, "eitc_limits": 2, "retire_conv_deduction": 2}


def _validate_overrides(base: dict, overrides: dict) -> dict:
    """개정 항목 검증 — 원본에 있는 키만·원본과 같은 모양(숫자/구간표)·음수 금지·율(rate)은 0~1·구간 상한 오름차순.

    고시값 자체를 판정하지 않는다(모양·자릿수 실수만 막는다). 값은 JSON 모양으로 저장(구간 상한 무한대=null).
    """
    if not overrides:
        raise ValidationError("바뀐 항목이 없습니다 — 개정이 없으면 '전년 승계'를 쓰세요")
    out: dict = {}
    for k, v in overrides.items():
        if k not in base:
            raise ValidationError(f"알 수 없는 항목입니다: {k}")
        b = base[k]
        if isinstance(b, list):
            if not isinstance(v, list) or not v:
                raise ValidationError(f"{k}: 구간표(행 목록)여야 합니다")
            width = len(b[0])
            rows = []
            for i, row in enumerate(v):
                if not isinstance(row, list) or len(row) != width:
                    raise ValidationError(f"{k}: {i + 1}행 칸 수가 {width}개여야 합니다")
                for j, x in enumerate(row):
                    if x is None and j == 0 and i == len(v) - 1:
                        continue                        # 마지막 구간 상한 = 무한대
                    if not isinstance(x, (int, float)) or isinstance(x, bool) or x < 0:
                        raise ValidationError(f"{k}: {i + 1}행 {j + 1}칸이 0 이상 숫자가 아닙니다")
                    if _TABLE_RATE_COL.get(k) == j and x > 1:
                        raise ValidationError(f"{k}: {i + 1}행 {j + 1}칸은 율 — 소수로 입력(예: 6% → 0.06)")
                rows.append(row)
            uppers = [r[0] for r in rows[:-1]]
            if rows[-1][0] is not None:
                raise ValidationError(f"{k}: 마지막 행 상한은 비워 두세요(무한대)")
            if any(a >= c for a, c in zip(uppers, uppers[1:])):
                raise ValidationError(f"{k}: 구간 상한이 오름차순이 아닙니다")
            out[k] = rows
        else:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or v < 0:
                raise ValidationError(f"{k}: 0 이상 숫자여야 합니다")
            if k in _RATE_KEYS and v > 1:
                raise ValidationError(f"{k}={v} — 율은 소수로 입력(예: 55% → 0.55)")
            out[k] = v
    return out


async def _drop_other_tax_kind(session: AsyncSession, *, year: int, keep: str) -> None:
    """같은 연도의 승계·개정 반영은 하나만 — 등록한 쪽을 남기고 다른 쪽 행은 지운다(어느 쪽이 쓰이는지 모호하지 않게)."""
    other = "TAX_CARRY" if keep == "TAX_AMEND" else "TAX_AMEND"
    row = await session.scalar(select(StatutoryParam).where(StatutoryParam.year == year, StatutoryParam.kind == other))
    if row is not None:
        await session.delete(row)
        await session.flush()


async def amend_tax(session: AsyncSession, *, year: int, from_year: int, overrides: dict, source_note: str,
                    actor: int) -> StatutoryParam:
    """세법 개정 반영 — 원본 연도 소득세 파라미터 위에 바뀐 항목만 등록(코드 배포 없이·근거 필수·감사)."""
    if from_year >= year:
        raise ValidationError("원본 연도는 대상 연도보다 이전이어야 합니다")
    if year in statutory.TAX_PARAMS:
        raise ValidationError(f"{year}년 소득세 파라미터는 코드에 이미 등록돼 있습니다")
    await statutory.refresh_db_params(session)
    base = statutory.tax_params_json(from_year)          # 원본이 실제로 등록돼 있어야 한다(아니면 예외)
    clean = _validate_overrides(base, overrides or {})
    # 같은 원본 위의 기존 개정분은 유지하고 이번 입력을 합친다 — 종전엔 통째로 바꿔 먼저 넣은 개정 항목이 조용히 빠졌다(검토 MED).
    # 원본 값으로 되돌린 항목은 개정에서 빠진다(되돌리기 경로).
    prev = await session.scalar(select(StatutoryParam).where(StatutoryParam.year == year,
                                                             StatutoryParam.kind == "TAX_AMEND"))
    merged = dict((prev.values.get("overrides") or {}) if prev is not None
                  and int(prev.values.get("from_year", -1)) == int(from_year) else {})
    merged.update(clean)
    changed = {k: v for k, v in merged.items() if k in base and v != base[k]}
    if not changed:
        raise ValidationError("원본과 같은 값뿐입니다 — 개정이 없으면 '전년 승계'를 쓰세요")
    await _drop_other_tax_kind(session, year=year, keep="TAX_AMEND")
    return await _upsert(session, year=year, kind="TAX_AMEND", values={"from_year": int(from_year), "overrides": changed},
                         source_note=source_note, actor=actor)


async def registry(session: AsyncSession, *, today) -> dict:
    """연도별 등록 현황 — 코드·관리화면 출처 구분, 내년 준비 상태."""
    await statutory.refresh_db_params(session)
    rows = {(r.year, r.kind): r for r in (await session.scalars(select(StatutoryParam))).all()}
    years = sorted(set(statutory.registered_years()["insurance"])
                   | set(statutory.registered_years()["tax"]) | {today.year, today.year + 1})
    out = []
    for y in years:
        ins_row, tax_row = rows.get((y, "INSURANCE")), rows.get((y, "TAX_CARRY"))
        amend_row = rows.get((y, "TAX_AMEND"))
        ins = (statutory._DB_INSURANCE.get(y) or statutory.RATES.get(y))
        out.append({
            "year": y,
            "insurance": ins,
            "insurance_source": "db" if ins_row else ("code" if y in statutory.RATES else None),
            "insurance_note": ins_row.source_note if ins_row else None,
            "code_insurance": statutory.RATES.get(y),
            "tax_source": ("code" if y in statutory.TAX_PARAMS
                           else f"amend:{amend_row.values['from_year']}" if amend_row
                           else f"carry:{tax_row.values['from_year']}" if tax_row else None),
            "tax_note": (amend_row or tax_row).source_note if (amend_row or tax_row) else None,
            "tax_overrides": sorted((amend_row.values.get("overrides") or {}).keys()) if amend_row else None,
        })
    ny = today.year + 1
    reg = statutory.registered_years()
    return {"years": out, "next_year": ny,
            "next_year_ready": {"insurance_rates": ny in reg["insurance"], "tax_params": ny in reg["tax"]},
            "keys": list(statutory.INSURANCE_KEYS),
            "optional_keys": (list(statutory.INSURANCE_OPTIONAL_KEYS) + list(statutory.EMPLOYER_OPTIONAL_KEYS)
                              + list(statutory.WAGE_OPTIONAL_KEYS))}


async def unconfirmed_payrolls(session: AsyncSession, *, year: int) -> int:
    """해당 연도 생성됐지만 미확정인 급여 건수 — 요율 정정 시 경고용(G6).

    확정(confirm) 시 사업주 부담금은 **그 시점 등록 요율**로 계산된다. 생성 후 요율을 고치고 확정하면
    근로자 공제(생성 시 요율)와 사업주 부담(새 요율)이 갈린다 — 이 건수가 0이 아니면 재생성 후 확정해야 한다.
    """
    from sqlalchemy import func

    from erp.hr.models import PayrollRecord
    return int(await session.scalar(select(func.count()).select_from(PayrollRecord).where(
        PayrollRecord.yyyymm.like(f"{year}%"), PayrollRecord.confirmed_at.is_(None))) or 0)


_RATE_CODES = (("NP", "국민연금"), ("HI", "건강보험"), ("LTC", "장기요양"), ("EI", "고용보험"))


async def diff_records(session: AsyncSession, *, yyyymm: str) -> dict:
    """급여월 기록 공제 ↔ 현재 요율 재계산 — 직원별 전체 목록(rate_diff 화면·요율 차액 정산 공용 단일 소스).

    보수월액은 기록된 건강보험 기준액(calc_basis.base — 생성 당시 보수월액)을 쓰고, 고용형태별 특례(일용직 미가입
    등)는 insurance()를 그대로 따른다. 기록에 없는 항목(적용 제외)은 비교하지 않는다. 쓰기 없음.
    """
    from erp.common.months import require_ym
    from erp.hr.models import Employee, PayrollLine, PayrollRecord
    yyyymm = require_ym(yyyymm)
    await statutory.refresh_db_params(session)
    year, month = int(yyyymm[:4]), statutory.month_of(yyyymm)
    recs = (await session.execute(
        select(PayrollRecord.id, PayrollRecord.employee_id, PayrollRecord.confirmed_at, PayrollRecord.paid_on,
               Employee.employee_no, Employee.employment_type, Employee.resigned_on)
        .join(Employee, Employee.id == PayrollRecord.employee_id)
        .where(PayrollRecord.yyyymm == yyyymm).order_by(Employee.employee_no))).all()
    lines = (await session.execute(
        select(PayrollLine.record_id, PayrollLine.item_code, PayrollLine.amount, PayrollLine.calc_basis)
        .where(PayrollLine.record_id.in_([r.id for r in recs]),
               PayrollLine.item_code.in_([c for c, _ in _RATE_CODES])))).all() if recs else []
    by_rec: dict[int, dict] = {}
    for ln in lines:
        by_rec.setdefault(ln.record_id, {})[ln.item_code] = ln
    totals = {c: {"recorded": 0, "recalculated": 0} for c, _ in _RATE_CODES}
    rows, skipped, no_basis = [], 0, 0
    for r in recs:
        mine = by_rec.get(r.id, {})
        hi = mine.get("HI") or mine.get("EI")
        base = (hi.calc_basis or {}).get("base") if hi is not None else None
        if not base:
            if mine:
                no_basis += 1                              # 공제는 있는데 기준액 기록이 없는 옛 행 — 비교 불가
            else:
                skipped += 1                               # 공제 라인 없음(무급·미가입)
            continue
        work_days = 8 if r.employment_type == "DAILY" and "HI" in mine else None   # 가입 여부는 기록을 따른다
        new = statutory.insurance(float(base), year=year, month=month,
                                  employment_type=r.employment_type, work_days=work_days)
        diff = {}
        for code, key in _RATE_CODES:
            old = int(mine[code].amount) if code in mine else 0
            now = int(new[key]) if code in mine else 0
            totals[code]["recorded"] += old
            totals[code]["recalculated"] += now
            if now != old:
                diff[code] = now - old
        if diff:
            rows.append({"employee_id": r.employee_id, "record_id": r.id, "employee_no": r.employee_no,
                         "base": int(base), "diff": diff, "total": sum(diff.values()),
                         "confirmed": r.confirmed_at is not None, "paid_on": r.paid_on,
                         "resigned_on": r.resigned_on})
    for t in totals.values():
        t["diff"] = t["recalculated"] - t["recorded"]
    return {"yyyymm": yyyymm, "year": year, "month": month, "records": len(recs), "skipped": skipped,
            "no_basis": no_basis, "totals": totals, "rows": rows}


async def rate_diff(session: AsyncSession, *, yyyymm: str, limit: int = 500) -> dict:
    """확정·생성된 급여의 4대보험 공제 ↔ **현재 등록 요율로 다시 계산한 값** 차액(읽기 전용).

    2026-10-10: 2026 코드 요율이 2025 복사본이라 그 사이 확정한 급여가 낮은 요율로 지급됐다. 차액 처리는
    '요율 차액 정산'(rate_settlement)에서 방식을 골라 실행한다. 이 조회는 급여·전표를 바꾸지 않는다.
    """
    d = await diff_records(session, yyyymm=yyyymm)
    rows = [{k: r[k] for k in ("employee_no", "base", "diff", "total", "confirmed", "paid_on")}
            for r in d["rows"][:limit]]
    return {"yyyymm": d["yyyymm"], "records": d["records"], "affected": len(d["rows"]),
            "skipped": d["skipped"], "no_basis": d["no_basis"],
            "rates": statutory.insurance_rates(d["year"]), "month": d["month"], "totals": d["totals"],
            "employee_diff_total": sum(t["diff"] for t in d["totals"].values()),
            "rows": rows,
            "note": "근로자 공제 기준 차액(사업주 부담 국민연금·건강·장기요양은 같은 금액이 추가로 발생). "
                    "급여·전표는 바뀌지 않습니다 — 처리 방법은 아래 '요율 차액 정산'에서 고르면 실행·결정 기록이 함께 남습니다."}
