"""근로소득 간이세액표(소득세법 시행령 별표 2) — 등록·대조·활성화·조회·계산 (v1.297.0·0262).

2026-10-10: 월 원천징수가 표가 아니라 근사 산식(연금보험료공제·특별소득공제 추정 누락)이라 확정 급여가
과다 원천징수됐다. 표는 법정 데이터 — 추출한 후보(CANDIDATE)를 관리자가 홈택스 원표와
대조해 출처를 적고 활성화(ACTIVE)한 것만 급여 계산에 쓴다. 활성 표가 없는 기간은 종전 산식(명세서에 '근사 산식' 표기).
"""
import csv
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.hr.models import WithholdingTable

DATA_DIR = Path(__file__).parent / "data"
CHILD_2026 = {"one": 20_830, "two": 45_830, "extra": 33_330}   # 별표 2 제3호(개정 2026.2.27)
MAX_FAMILY = 11
_audit = AuditService()


def read_csv(path: Path) -> list[list[int]]:
    """패키지 데이터 CSV(#주석·헤더 lo_k,hi_k,f1..f11) → 정수 행."""
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.reader(line for line in f if not line.startswith("#")):
            if r and r[0] != "lo_k":
                rows.append([int(x) for x in r])
    return rows


def checksum(rows: list[list[int]]) -> str:
    return hashlib.sha256(json.dumps(rows, separators=(",", ":")).encode()).hexdigest()


def validate(rows: list[list[int]]) -> None:
    """구조 검증 — 13열·구간 연속·급여 단조 증가·가족 수 단조 감소·1천만원 정확값 행. 실패는 422(값 대조는 사람)."""
    if not rows or any(len(r) != 2 + MAX_FAMILY for r in rows):
        raise ValidationError("간이세액표 행은 [이상, 미만, 가족 1~11명 세액] 13열이어야 합니다")
    body = [r for r in rows if r[0] != r[1]]
    top = [r for r in rows if r[0] == r[1]]
    if len(top) != 1 or top[0][0] != 10_000 or body[-1][1] != 10_000:
        raise ValidationError("월급여 1천만원(10,000천원) 정확값 행과 그 직전 구간이 필요합니다")
    for a, b in zip(body, body[1:]):
        if a[1] != b[0]:
            raise ValidationError(f"구간이 끊겼습니다: {a[1]}천원 ↔ {b[0]}천원")
        if any(b[i] < a[i] for i in range(2, 13)):
            raise ValidationError(f"{b[0]}천원 구간 세액이 직전 구간보다 작습니다(급여 단조성)")
    for r in rows:
        if any(r[i + 1] > r[i] for i in range(2, 12)):
            raise ValidationError(f"{r[0]}천원 구간 세액이 가족 수가 늘수록 커집니다(가족 단조성)")


def _snapshot(t: WithholdingTable) -> dict:
    return {"id": t.id, "effective_from": t.effective_from, "title": t.title, "rows": t.rows,
            "child_deduction": t.child_deduction, "status": t.status}


async def get_table(session: AsyncSession, table_id: int) -> dict | None:
    """특정 표(상태 무관) — 이미 기록된 소득세가 쓴 표를 다시 쓸 때(성과급 A·C 같은 기준)."""
    t = await session.get(WithholdingTable, table_id)
    return _snapshot(t) if t is not None else None


async def load_active(session: AsyncSession, *, on: date) -> dict | None:
    """기준일(급여월 1일) 시점 활성 표 — 시행일 ≤ 기준일 중 최신. 없으면 None(종전 산식)."""
    t = await session.scalar(select(WithholdingTable).where(
        WithholdingTable.status == "ACTIVE", WithholdingTable.effective_from <= on)
        .order_by(WithholdingTable.effective_from.desc()).limit(1))
    return _snapshot(t) if t is not None else None


def _base_amount(rows: list[list[int]], monthly: float, family: int) -> int:
    """표 본문 세액(자녀 차감 전) — 별표 2 제4호(11명 초과)·제6호(1천만원 초과 계산식·시행 2026.3.1)."""
    if family > MAX_FAMILY:                      # 제4호: 11명 세액 − (10명 − 11명) × 초과 인원
        a = _base_amount(rows, monthly, MAX_FAMILY)
        b = _base_amount(rows, monthly, MAX_FAMILY - 1)
        return max(a - (b - a) * (family - MAX_FAMILY), 0)
    col = 1 + max(family, 1)
    k = monthly / 1000
    if monthly <= 10_000_000:
        if k >= 10_000:                          # 정확히 1천만원
            return next(r for r in rows if r[0] == r[1])[col]
        for r in rows:
            if r[0] != r[1] and r[0] <= k < r[1]:
                return r[col]
        return 0                                  # 표 최저 구간 미만(77만원 미만)
    base = next(r for r in rows if r[0] == r[1])[col]
    if monthly <= 14_000_000:
        return int(base + (monthly - 10_000_000) * 0.98 * 0.35 + 25_000)
    if monthly <= 28_000_000:
        return int(base + 1_397_000 + (monthly - 14_000_000) * 0.98 * 0.38)
    if monthly <= 30_000_000:
        return int(base + 6_610_600 + (monthly - 28_000_000) * 0.98 * 0.40)
    if monthly <= 45_000_000:
        return int(base + 7_394_600 + (monthly - 30_000_000) * 0.40)
    if monthly <= 87_000_000:
        return int(base + 13_394_600 + (monthly - 45_000_000) * 0.42)
    return int(base + 31_034_600 + (monthly - 87_000_000) * 0.45)


def compute(table: dict, monthly_taxable: float, *, family: int, children: int = 0) -> dict:
    """월 소득세(간이세액표 100%) = 표 세액 − 8~20세 자녀 차감(음수면 0) · 지방소득세 10%(10원 미만 절사)."""
    tax = _base_amount(table["rows"], max(monthly_taxable, 0), family)
    c = table.get("child_deduction") or CHILD_2026
    if children >= 1:
        tax -= c["one"] if children == 1 else c["two"] + (children - 2) * c["extra"]
    tax = max(int(tax), 0)
    tax = tax // 10 * 10
    return {"소득세": tax, "지방소득세": (tax // 10) // 10 * 10}


def children_8_20(dependents: list, year: int) -> int:
    """부양가족 등록에서 귀속연도 기준 8~20세 자녀 수(공제 산입분만)."""
    return sum(1 for d in dependents if d.relation == "CHILD" and d.counted and d.birth_date
               and 8 <= year - d.birth_date.year <= 20)


def family_from(deps: list, profile_dependents: int | None) -> int:
    """공제대상가족 수 — 부양가족 등록이 있으면 본인 1 + 공제 산입 가족(급여 개별 생성 _resolve_pay_basis와 같은 규칙),
    없으면 보수 프로필 값(기본 1). 자녀 차감은 같은 등록에서 세므로 가족 수가 자녀 수보다 작아질 수 없다."""
    if deps:
        return 1 + sum(1 for d in deps if d.counted)
    return max(int(profile_dependents or 1), 1)


async def employee_basis(session: AsyncSession, employee_id: int, *, on: date) -> tuple[dict | None, int, int]:
    """(활성 표|None, 공제대상가족 수, 8~20세 자녀 수) — 성과급·퇴사 정산 등 급여 밖 원천징수의 단일 근거."""
    from erp.hr.models import CompensationProfile, Dependent
    deps = (await session.scalars(select(Dependent).where(Dependent.employee_id == employee_id))).all()
    prof = await session.scalar(select(CompensationProfile.dependents).where(
        CompensationProfile.employee_id == employee_id))
    return await load_active(session, on=on), family_from(deps, prof), children_8_20(deps, on.year)


async def list_tables(session: AsyncSession) -> list[dict]:
    rows = (await session.scalars(select(WithholdingTable).order_by(
        WithholdingTable.effective_from.desc(), WithholdingTable.id.desc()))).all()
    return [{"id": t.id, "effective_from": t.effective_from, "status": t.status, "title": t.title,
             "source_note": t.source_note, "row_count": len(t.rows or []), "checksum": t.checksum[:12],
             "activated_at": t.activated_at, "child_deduction": t.child_deduction,
             "samples": samples(t.rows)} for t in rows]


_SAMPLE_K = (1_500, 2_000, 2_500, 3_000, 3_500, 4_000, 5_000, 6_000, 8_000, 10_000)


def samples(rows: list[list[int]]) -> list[list[int]]:
    """대조용 표본 칸 — 홈택스 원표의 같은 구간과 눈으로 맞춰 보는 행(월 150만~1천만)."""
    out = []
    for k in _SAMPLE_K:
        r = next((r for r in rows if (r[0] <= k < r[1]) or (r[0] == r[1] == k)), None)
        if r is not None:
            out.append(r)
    return out


async def activate(session: AsyncSession, *, actor: int, table_id: int, source_note: str) -> dict:
    """후보 → 활성. 출처(대조 근거) 필수·구조·체크섬 재검증·같은 시행일 기존 활성은 RETIRED. 감사."""
    note = (source_note or "").strip()
    if len(note) < 8:
        raise ValidationError("대조 근거(예: '홈택스 2026.3.1 시행 원표 표본 10칸 대조 일치')를 8자 이상 입력하세요")
    t = await session.scalar(select(WithholdingTable).where(WithholdingTable.id == table_id).with_for_update())
    if t is None or t.status != "CANDIDATE":
        raise ValidationError("활성화할 수 있는 후보 표가 아닙니다")
    validate(t.rows)
    if checksum(t.rows) != t.checksum:
        raise ValidationError("표 데이터가 등록 이후 바뀌었습니다(체크섬 불일치) — 다시 등록하세요")
    for old in (await session.scalars(select(WithholdingTable).where(
            WithholdingTable.status == "ACTIVE", WithholdingTable.effective_from == t.effective_from))).all():
        old.status = "RETIRED"
    await session.flush()
    t.status, t.source_note = "ACTIVE", note[:500]
    t.activated_by, t.activated_at = actor, datetime.now(timezone.utc)
    from sqlalchemy.exc import IntegrityError
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError:                     # 같은 시행일 동시 활성화 경합 패자(uq_wht_active)
        raise ValidationError("같은 시행일 표가 동시에 활성화됐습니다 — 목록을 새로 고쳐 확인하세요") from None
    await _audit.record_audit(session, actor=actor, action="ACTIVATE", entity="hr.withholding_tables",
                              target_id=str(t.id), after={"effective_from": str(t.effective_from),
                                                          "checksum": t.checksum, "source_note": t.source_note})
    await session.flush()
    return {"id": t.id, "status": t.status, "effective_from": t.effective_from}


async def discard(session: AsyncSession, *, actor: int, table_id: int, reason: str) -> dict:
    """후보 폐기(CANDIDATE → RETIRED) — 원표 대조에서 틀린 후보·체크섬 불일치로 활성화가 거부된 후보를 치운다.

    종전엔 후보가 활성화로만 끝나 틀린 후보가 결정·조치함에 계속 남았다(2026-10-11 검토). 사유 필수·감사.
    활성 표는 여기서 내리지 않는다(같은 시행일 새 표 활성화가 대체한다).
    """
    why = (reason or "").strip()
    if len(why) < 8:
        raise ValidationError("폐기 사유(예: '홈택스 원표 300만 구간 가족 2명 값 불일치')를 8자 이상 입력하세요")
    t = await session.scalar(select(WithholdingTable).where(WithholdingTable.id == table_id).with_for_update())
    if t is None or t.status != "CANDIDATE":
        raise ValidationError("폐기할 수 있는 후보 표가 아닙니다")
    t.status, t.source_note = "RETIRED", f"후보 폐기: {why}"[:500]
    await _audit.record_audit(session, actor=actor, action="RETIRE", entity="hr.withholding_tables",
                              target_id=str(t.id), before={"status": "CANDIDATE"},
                              after={"status": "RETIRED", "reason": why[:300]})
    await session.flush()
    return {"id": t.id, "status": t.status, "effective_from": t.effective_from}


async def _retax_plan(session: AsyncSession, table_id: int, *, lock: bool = False) -> dict:
    """세액 재계산 대상 — 활성 표가 계산에 쓰여야 하는 달의 **미확정** 급여 중 소득세 줄이 그 표 기준이 아닌 것.

    활성화 전에 만든 미확정 급여는 근사 산식으로 계산된 채 남는다(재생성해야 바뀌는데 일괄 재생성 화면이 없고, 개별 재생성은
    수기 입력값을 덮는다). 소득세·지방소득세 줄만 **생성과 같은 입력**(과세 월급여액·가족 수=부양가족 등록 기준·자녀)으로 다시 계산한다.
    대상: 근사 산식(`근사산식`·v1.297 이전 '간이세액표' 오표기) · 다른 표(옛 연도·같은 시행일 정정 전 표)로 계산된 것.
    제외(개별 확인): 수기 입력 소득세 · 성과급 합산 원천징수(INCTAX — 상여세가 기존 소득세 기준) · 소득세 줄 없는데 표 세액이 있는 것.
    무시: 확정분(전표 전기됨) · 일용 분리과세 · 소득세 줄 없고 표 세액도 0.
    """
    from erp.hr.models import CompensationProfile, Dependent, Employee, PayItemCatalog, PayrollLine, PayrollRecord
    t = await session.get(WithholdingTable, table_id)
    if t is None or t.status != "ACTIVE":
        raise ValidationError("활성 간이세액표에서만 세액을 재계산할 수 있습니다")
    table = _snapshot(t)
    actives = (await session.scalars(select(WithholdingTable).where(WithholdingTable.status == "ACTIVE")
                                     .order_by(WithholdingTable.effective_from.desc()))).all()

    def table_for(yyyymm: str) -> int | None:
        first = date(int(yyyymm[:4]), int(yyyymm[-2:]), 1)
        return next((a.id for a in actives if a.effective_from <= first), None)

    q = (select(PayrollRecord, Employee.employee_no).join(Employee, Employee.id == PayrollRecord.employee_id)
         .where(PayrollRecord.confirmed_at.is_(None),
                PayrollRecord.yyyymm >= t.effective_from.strftime("%Y-%m"))     # 정규형 'YYYY-MM' 사전순=시간순
         .order_by(PayrollRecord.yyyymm, Employee.employee_no))
    if lock:
        q = q.with_for_update(of=PayrollRecord)
    recs = [(r, no) for r, no in (await session.execute(q)).all() if table_for(r.yyyymm) == t.id]
    ids = [r.id for r, _ in recs]
    emp_ids = {r.employee_id for r, _ in recs}
    lines: dict[int, dict[str, PayrollLine]] = {}
    for ln in (await session.scalars(select(PayrollLine).where(PayrollLine.record_id.in_(ids)))).all() if ids else []:
        lines.setdefault(ln.record_id, {})[ln.item_code] = ln
    deps: dict[int, list] = {}
    for d in (await session.scalars(select(Dependent).where(
            Dependent.employee_id.in_(emp_ids)))).all() if recs else []:
        deps.setdefault(d.employee_id, []).append(d)
    profile_deps = dict((await session.execute(select(CompensationProfile.employee_id, CompensationProfile.dependents)
                                                .where(CompensationProfile.employee_id.in_(emp_ids)))).all()) if recs else {}
    nontax = {c: float(lim or 0) for c, lim in (await session.execute(select(
        PayItemCatalog.code, PayItemCatalog.nontax_limit).where(
        PayItemCatalog.kind == "EARNING", PayItemCatalog.taxable.is_(False)))).all()}

    def taxable_from_lines(ls: dict) -> float:
        # v1.297 이전 기록은 근거에 과세 월급여액이 없다 — 생성과 같은 식(지급 줄 − 비과세·성과급 제외)으로 복원(영향 미리보기와 동일)
        amt = 0.0
        for ln in ls.values():
            if ln.item_kind == "EARNING" and ln.item_code != "INCENTIVE":
                a = float(ln.amount)
                if ln.item_code in nontax:
                    a -= min(a, nontax[ln.item_code]) if nontax[ln.item_code] else a
                amt += a
        return max(amt, 0.0)

    targets, skipped = [], []
    for r, no in recs:
        ls = lines.get(r.id, {})
        tax = ls.get("TAX")
        basis = (tax.calc_basis or {}) if tax is not None else {}
        method = basis.get("method")
        if method == "일용분리과세":
            continue
        if tax is not None and method == "간이세액표" and basis.get("table_id") is not None \
                and int(basis["table_id"]) == t.id:
            continue                                       # 이미 이 표 기준
        # 생성과 같은 입력 — 가족 수=부양가족 등록(없으면 프로필)·자녀=등록 8~20세(가족−1 이하)
        dl = deps.get(r.employee_id, [])
        fam = family_from(dl, profile_deps.get(r.employee_id))
        kids = min(children_8_20(dl, int(r.yyyymm[:4])), fam - 1)
        taxable = float(basis["taxable"]) if basis.get("taxable") is not None else taxable_from_lines(ls)
        new = compute(table, taxable, family=fam, children=kids)
        machine = method in ("근사산식", "간이세액표")          # 생성이 만든 줄(수기 PUT은 근거가 없거나 다름)
        reason = ("성과급 합산 원천징수 포함 — 직원 화면에서 급여 재생성(소득세가 표 기준) 다음에 성과급 취소 후 다시 산정·검토·승인(상여세가 새 소득세 기준)" if "INCTAX" in ls
                  else ("소득세 줄 없음 — 직원 화면에서 재생성" if new["소득세"] else "") if tax is None
                  else "수기 입력 소득세 — 개별 확인" if not machine else None)
        if reason == "":
            continue                                       # 줄 없음·표 세액 0 — 재생성해도 같다
        if reason:
            # counted: 재생성으로 풀리는 것만 결정·조치함 건수에(수기 입력은 사람이 넣은 값 — 영구 발화 방지)
            skipped.append({"employee_no": no, "yyyymm": r.yyyymm, "reason": reason, "counted": machine or tax is None})
            continue
        local = ls.get("LOCALTAX")
        targets.append({"record": r, "tax": tax, "local": local, "employee_no": no, "yyyymm": r.yyyymm,
                        "taxable": int(taxable), "family": fam, "children": kids,
                        "recorded_family": basis.get("dependents"),
                        "recorded": int(tax.amount) + (int(local.amount) if local is not None else 0),
                        "new": new["소득세"] + new["지방소득세"], "new_tax": new["소득세"], "new_local": new["지방소득세"]})
    return {"table": table, "targets": targets, "skipped": skipped}


def _retax_summary(plan: dict, limit: int = 300) -> dict:
    tg = plan["targets"]
    return {"table_id": plan["table"]["id"], "targets": len(tg),
            "diff_total": sum(x["new"] - x["recorded"] for x in tg),
            "rows": [{k: x[k] for k in ("employee_no", "yyyymm", "taxable", "family", "recorded_family", "children",
                                        "recorded", "new")}
                     | {"diff": x["new"] - x["recorded"]} for x in tg[:limit]],
            "skipped": plan["skipped"][:limit], "skipped_count": len(plan["skipped"]),
            "note": "소득세+지방소득세 합계. 미확정 급여의 두 줄만 바꾸고 지급·다른 공제 줄은 그대로입니다. "
                    "가족 수는 급여 생성과 같이 부양가족 등록 기준(기록과 다르면 괄호에 기록 값)."}


async def retax_preview(session: AsyncSession, *, table_id: int) -> dict:
    return _retax_summary(await _retax_plan(session, table_id))


async def pending_retax_count(session: AsyncSession, *, table_id: int) -> tuple[int, int]:
    """(재계산 대상, 개별 확인 필요) — 결정·조치함 항목. 둘 다 처리(재계산·재생성·확정)하면 0이 된다.

    수기 입력 소득세는 세지 않는다 — 사람이 일부러 넣은 값이고, 외부 · 시범 기록(근거 없는 정액 소득세)처럼 확정되지 않는 기록이면 항목이 영구히 남는다(영구 발화 금지).
    미리보기의 제외 목록에는 그대로 보인다.
    """
    p = await _retax_plan(session, table_id)
    return len(p["targets"]), sum(1 for x in p["skipped"] if x["counted"])


async def retax(session: AsyncSession, *, actor: int, table_id: int, note: str) -> dict:
    """미확정 급여 소득세·지방소득세 줄을 활성 표로 재계산(행 잠금·사유 필수·감사). 다른 줄·확정분은 건드리지 않는다."""
    from erp.hr.models import PayrollLine
    from erp.hr.payroll_rules import table_tax_basis
    why = (note or "").strip()
    if len(why) < 4:
        raise ValidationError("재계산 사유를 4자 이상 입력하세요")
    plan = await _retax_plan(session, table_id, lock=True)
    out = _retax_summary(plan)
    for x in plan["targets"]:
        basis = table_tax_basis(plan["table"], x["taxable"], x["family"], x["children"])
        if x["new_tax"]:
            x["tax"].amount, x["tax"].calc_basis = x["new_tax"], basis
        else:
            await session.delete(x["tax"])             # 표 세액 0 — 생성 때와 같이 줄을 두지 않는다
        if x["new_local"]:
            if x["local"] is not None:
                x["local"].amount = x["new_local"]
            else:
                session.add(PayrollLine(record_id=x["record"].id, item_code="LOCALTAX", item_kind="DEDUCTION",
                                        amount=x["new_local"], calc_basis={"formula": "소득세 × 10%", "rate": 0.1}))
        elif x["local"] is not None:
            await session.delete(x["local"])
    await _audit.record_audit(session, actor=actor, action="RETAX", entity="hr.payroll_records",
                              target_id=f"withholding-table:{table_id}",
                              after={"targets": out["targets"], "diff_total": out["diff_total"], "note": why[:300],
                                     "records": [x["record"].id for x in plan["targets"]][:500]})
    await session.flush()
    return out


async def impact(session: AsyncSession, *, table_id: int, yyyymm: str, limit: int = 300) -> dict:
    """영향 미리보기(읽기 전용) — 그 달 급여의 기록 소득세(TAX) ↔ 이 표로 계산한 세액. 직원별·합계."""
    from erp.common.months import require_ym
    from erp.hr.models import Dependent, Employee, PayItemCatalog, PayrollLine, PayrollRecord
    yyyymm = require_ym(yyyymm)
    t = await session.get(WithholdingTable, table_id)
    if t is None:
        raise ValidationError("간이세액표가 없습니다")
    table = _snapshot(t)
    nontax = {c: float(lim or 0) for c, lim in (await session.execute(select(
        PayItemCatalog.code, PayItemCatalog.nontax_limit).where(
        PayItemCatalog.kind == "EARNING", PayItemCatalog.taxable.is_(False)))).all()}
    recs = (await session.execute(
        select(PayrollRecord.id, PayrollRecord.employee_id, PayrollRecord.confirmed_at, Employee.employee_no)
        .join(Employee, Employee.id == PayrollRecord.employee_id)
        .where(PayrollRecord.yyyymm == yyyymm).order_by(Employee.employee_no))).all()
    ids = [r.id for r in recs]
    lines = (await session.execute(select(PayrollLine.record_id, PayrollLine.item_code, PayrollLine.item_kind,
                                          PayrollLine.amount, PayrollLine.calc_basis)
                                   .where(PayrollLine.record_id.in_(ids)))).all() if ids else []
    deps: dict[int, list] = {}
    for d in (await session.scalars(select(Dependent).where(
            Dependent.employee_id.in_([r.employee_id for r in recs])))).all() if recs else []:
        deps.setdefault(d.employee_id, []).append(d)
    by: dict[int, dict] = {}
    for ln in lines:
        b = by.setdefault(ln.record_id, {"taxable": 0.0, "tax": None, "family": None})
        if ln.item_kind == "EARNING" and ln.item_code not in ("INCENTIVE",):
            amt = float(ln.amount)
            if ln.item_code in nontax:
                amt -= min(amt, nontax[ln.item_code]) if nontax[ln.item_code] else amt
            b["taxable"] += amt
        if ln.item_code == "TAX":
            b["tax"] = int(ln.amount)
            b["family"] = (ln.calc_basis or {}).get("dependents")
            b["daily"] = (ln.calc_basis or {}).get("method") == "일용분리과세"
    year = int(yyyymm[:4])
    rows, total, n = [], 0, 0
    for r in recs:
        b = by.get(r.id)
        if not b or b["tax"] is None or b.get("daily"):
            continue                              # 일용직(분리과세)은 간이세액표 대상 아님
        n += 1
        fam = int(b["family"] or family_from(deps.get(r.employee_id, []), None))
        kids = min(children_8_20(deps.get(r.employee_id, []), year), max(fam - 1, 0))
        new = compute(table, b["taxable"], family=fam, children=kids)["소득세"]
        diff = new - b["tax"]
        total += diff
        if diff and len(rows) < limit:
            rows.append({"employee_no": r.employee_no, "taxable": int(b["taxable"]), "family": fam, "children": kids,
                         "recorded": b["tax"], "table": new, "diff": diff, "confirmed": r.confirmed_at is not None})
    diffs = [x["diff"] for x in rows]
    return {"yyyymm": yyyymm, "table_id": t.id, "table_status": t.status, "records": n, "diff_total": total,
            "mean": round(total / n) if n else 0, "min": min(diffs) if diffs else 0, "max": max(diffs) if diffs else 0,
            "rows": rows,
            "note": "음수=표 기준보다 더 뗐음(과다 원천징수). 소득세만(지방소득세는 10% 별도). 급여·전표는 바뀌지 않습니다."}
