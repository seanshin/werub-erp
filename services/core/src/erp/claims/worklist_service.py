"""보험심사 워크리스트 (PR-A) — 사전심사 결과의 케이스 영속·검토 워크플로.

케이스 = (chart_no, 진료월) 멱등. 재실행 시:
- 동일 적발(rule_code, code, detail 키)은 유지(결정 보존), 미재현 적발은 resolved 마킹
- FIX_REQUESTED 케이스의 미해소 적발이 전부 해소되면 RESOLVED 자동 전이
- 종결(CLEARED/RESOLVED) 케이스에 새 적발 발생 시 OPEN 재개
위험액 = 미해소(PENDING/ACCEPT·미resolved) 적발 코드들의 청구액 합(코드 중복 제거).
"""
from datetime import datetime, timezone

from sqlalchemy import case as sa_case
from sqlalchemy import func, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.claims.models import (
    ClaimAdjustment, ClaimStatement, PreReviewCase, PreReviewComment, PreReviewFinding,
    PreReviewQuery, PreReviewReply, PreReviewRule,
)

_audit = AuditService()


async def resolve_fee_with_mapping(session: AsyncSession, codes: set[str], as_of,
                                   grade: str | None = None) -> dict:
    """청구 코드 → {price,cap,type} fee 맵 (CONFIRMED 약품매핑 적용 — 사전심사 공용).

    grade 지정 시 종별가산율(institution_surcharge_rates) 적용대상 code_type의 단가에 가산
    (BASE_CATALOG 기본수가 → ERP 가산, #75). 가산율 미적재 시 무영향. AMOUNT_OUTLIER도 가산 단가 기준.

    HIS 비표준코드를 보험코드로 변환해 fee 멤버십 해소(UNKNOWN_CODE 미등재 오탐 방지).
    매핑 약가가 진료일에 미시행이면 현행(valid_to NULL) 폴백(billing_service 일관 #181).
    수동 사전심사(/pre-review)·자동 sync 양 경로가 동일 규칙을 쓰도록 단일화(중복 결함 재발 방지).
    """
    from sqlalchemy import or_

    from erp.regulatory.drug_map_service import DrugMapService
    from erp.regulatory.models import FeeMaster
    if not codes:
        return {}
    cmap = await DrugMapService.confirmed_map(session)
    mapped = {cmap[c] for c in codes if c in cmap}
    allc = codes | mapped
    _cols = (FeeMaster.code, FeeMaster.unit_price, FeeMaster.price_cap, FeeMaster.code_type,
             FeeMaster.sex, FeeMaster.min_age, FeeMaster.max_age)
    rows = (await session.execute(
        select(*_cols)
        .where(FeeMaster.code.in_(allc), FeeMaster.valid_from <= as_of,
               or_(FeeMaster.valid_to.is_(None), FeeMaster.valid_to > as_of)))).all()
    fee = {c: {"price": float(up or 0), "cap": float(pc) if pc is not None else None,
               "type": ct, "sex": sx, "min_age": mn, "max_age": mx}
           for c, up, pc, ct, sx, mn, mx in rows}
    if mapped:   # 매핑 보험코드가 진료일에 미시행이어도 현행 버전으로 식별
        cur = (await session.execute(
            select(*_cols).where(FeeMaster.code.in_(mapped), FeeMaster.valid_to.is_(None)))).all()
        for c, up, pc, ct, sx, mn, mx in cur:
            fee.setdefault(c, {"price": float(up or 0),
                               "cap": float(pc) if pc is not None else None, "type": ct,
                               "sex": sx, "min_age": mn, "max_age": mx})
    for his_c, ins_c in cmap.items():   # HIS코드를 보험코드 fee로 인식(미등재 오탐 방지)
        if his_c not in fee and ins_c in fee:
            fee[his_c] = fee[ins_c]
    exempt = await DrugMapService.exempt_codes(session)   # 약가 비대상(혈액제제 등)
    for c in codes & exempt:   # 수가 마스터 미등재가 정상 — UNKNOWN_CODE 제외(membership만, 단가는 HIS)
        fee.setdefault(c, {"price": 0.0, "cap": None, "type": "EXEMPT"})
    if grade:   # 종별가산(G6) — 적용대상 code_type 단가에 가산(미적재 시 무영향)
        from erp.regulatory.models import InstitutionSurchargeRate
        sr = await session.scalar(select(InstitutionSurchargeRate).where(
            InstitutionSurchargeRate.grade == grade, InstitutionSurchargeRate.active.is_(True)))
        if sr and float(sr.rate):
            targets = {t.strip().upper() for t in (sr.applies_to or "TARIFF").split(",") if t.strip()}
            # 방어적 상한 — 로더는 [0,1.0] 검증하나 직접 설정 행 대비(>100% 가산 폭주 차단)
            mult = 1.0 + max(0.0, min(float(sr.rate), 1.0))
            for v in fee.values():
                if isinstance(v, dict) and (v.get("type") or "TARIFF").upper() in targets \
                        and v.get("price"):
                    v["price"] = round(float(v["price"]) * mult)
    return fee


# 상태 전이 규칙 — 역행·비약 차단 (RESOLVED/CLEARED는 재검토 재개만 허용)
TRANSITIONS = {
    "OPEN": {"IN_REVIEW", "CLEARED", "BLOCKED", "FIX_REQUESTED"},
    "IN_REVIEW": {"OPEN", "CLEARED", "FIX_REQUESTED", "BLOCKED"},
    "FIX_REQUESTED": {"IN_REVIEW", "CLEARED", "RESOLVED"},
    "BLOCKED": {"IN_REVIEW", "FIX_REQUESTED", "CLEARED"},
    "CLEARED": {"IN_REVIEW"},
    "RESOLVED": {"IN_REVIEW"},
}

# 종결(이상없음/해소) 상태 — 미해소 BLOCK(삭감위험) 적발이 살아있으면 수동 종결 금지.
# P1 자동경로(save_run)는 finding_count==0에서만 CLEARED/RESOLVED로 종결하는데(불변식),
# 수동경로(update_case/bulk_status)는 전이규칙만 검사해 BLOCK 적발을 조용히 종결할 수 있던
# 비대칭을 차단(수동·자동 대칭). 종결하려면 적발을 먼저 해소·수정하거나 오탐 처리(사유 기재).
_TERMINAL_CLOSE = {"CLEARED", "RESOLVED"}

_FKEY = ("rule_code", "code", "treated_on", "detail")   # 진료일 포함 — 날짜 다른 동일적발 구분


def _finding_key(f) -> tuple:
    if isinstance(f, dict):
        return tuple(f.get(k) or "" for k in _FKEY)
    return tuple(getattr(f, k, "") or "" for k in _FKEY)


class WorklistService:
    async def save_run(self, session: AsyncSession, *, actor: int,
                       lines: list[dict], findings: list) -> dict:
        """평가 결과 영속 — 차트별 케이스 upsert + 적발 동기화(해소 마킹)."""
        now = datetime.now(timezone.utc)
        # 차트별 라인 집계 (care_month·청구액·코드별 금액)
        charts: dict[str, dict] = {}
        for ln in lines:
            chart = ln.get("chart_no") or ""
            if not chart:
                continue
            c = charts.setdefault(chart, {"amt": 0.0, "by_code": {}, "month": None,
                                          "enc": set(), "depts": {}})
            if ln.get("encounter_type"):
                c["enc"].add(ln["encounter_type"])
            amt = float(ln.get("amount") or 0)
            c["amt"] += amt
            dept = ln.get("dept_code")
            if dept:                                  # 부서 경계(IDOR 가드)용 — 청구액 가중
                c["depts"][dept] = c["depts"].get(dept, 0.0) + amt
            code = ln.get("code")
            if code:
                c["by_code"][code] = c["by_code"].get(code, 0.0) + amt
            d = ln.get("treated_on")
            if d is not None and c["month"] is None:
                c["month"] = f"{d.year:04d}-{d.month:02d}"

        by_chart: dict[str, list] = {}
        for f in findings:
            by_chart.setdefault(f.chart_no or "", []).append(f)

        from erp.config import get_settings
        s = get_settings()
        auto_clear_on = s.prereview_auto_clear   # P1 킬스위치
        # P2 — 확정 오탐 패턴(인간 전건 오탐) 자동 DISMISS (옵트인)
        patterns = (await self._auto_dismiss_patterns(
            session, min_sample=s.prereview_auto_dismiss_min_sample)
            if s.prereview_auto_dismiss else set())
        # 방어심층(의료법 게이트) — 자동 DISMISS 대상 룰 중 physician_gated는 배제(패턴 제외와 이중화).
        # 신규 적발의 rule_code 집합만 1회 조회(N+1 회피). gated면 자동처리 스킵·인간 확인 대기.
        gated_rules: set[str] = set()
        if patterns:
            from erp.claims.models import PreReviewRule
            rcs = {f.rule_code for f in findings}
            gated_rules = {rc for rc in (await session.execute(
                select(PreReviewRule.code).where(PreReviewRule.code.in_(rcs),
                                                 PreReviewRule.physician_gated.is_(True)))).scalars()}
        saved, resolved_cases, auto_cleared, auto_dismissed = [], 0, 0, 0
        for chart, agg in charts.items():
            month = agg["month"] or now.strftime("%Y-%m")
            new_findings = by_chart.get(chart, [])
            case = (await session.execute(
                select(PreReviewCase).where(PreReviewCase.chart_no == chart,
                                            PreReviewCase.care_month == month))
            ).scalar_one_or_none()
            if case is None:
                if not new_findings:
                    continue                      # 적발 없는 신규 차트는 케이스 생성 안 함
                case = PreReviewCase(chart_no=chart, care_month=month, status="OPEN")
                session.add(case)
                await session.flush()

            existing = list((await session.scalars(
                select(PreReviewFinding).where(PreReviewFinding.case_id == case.id))).all())
            new_keys = {_finding_key(f) for f in new_findings}
            old_keys = set()
            for ex in existing:
                k = _finding_key(ex)
                old_keys.add(k)
                ex.resolved = k not in new_keys   # 미재현 = 해소 (결정은 보존)
            added_findings = []                    # 신규 적발 — rollup 재조회 없이 재사용
            for f in new_findings:
                if _finding_key(f) not in old_keys:
                    amount = agg["by_code"].get(f.code or "", 0.0)
                    nf = PreReviewFinding(
                        case_id=case.id, rule_code=f.rule_code, rule_name=f.rule_name,
                        severity=f.severity, code=f.code, treated_on=f.treated_on,
                        detail=f.detail, amount=amount,
                        reason_group=getattr(f, "reason_group", "") or None,
                        reason_code=getattr(f, "reason_code", "") or None)
                    if (f.rule_code, f.code) in patterns and f.rule_code not in gated_rules:
                        # P2 — 확정 오탐 패턴 자동 DISMISS(가역·감사). gated 룰은 이중 배제(의료법).
                        nf.decision, nf.auto_decided = "DISMISS", True
                        nf.decided_by, nf.decided_at = actor, now
                        nf.decision_note = ("자동 오탐(캘리브레이션: 동일 룰·코드 인간 "
                                            f"전건 오탐 {s.prereview_auto_dismiss_min_sample}건↑)")
                        auto_dismissed += 1
                        await _audit.record_audit(session, actor=actor, action="AUTO_DISMISS",
                                                  entity="claims.pre_review_findings",
                                                  target_id=f"{f.rule_code}:{f.code}",
                                                  after={"auto": True})
                    session.add(nf)
                    added_findings.append(nf)
            await session.flush()

            if agg["enc"]:    # 입원/외래 승격 — 혼재 시 MIXED (재실행마다 최신화)
                case.encounter_type = (agg["enc"].pop() if len(agg["enc"]) == 1
                                       else "MIXED")
            if agg["depts"]:  # 부서 경계 — 청구액 우세 부서로 dept_code 최신화(IDOR 가드 실효)
                case.dept_code = max(agg["depts"], key=agg["depts"].get)
            await self._refresh_rollup(session, case, claimed_amt=agg["amt"], now=now,
                                       findings=existing + added_findings)
            if case.status in ("CLEARED", "RESOLVED") and case.finding_count > 0:
                case.status, case.auto_cleared = "OPEN", False   # 종결 후 새 적발 — 재개(자동표시 해제)
            elif case.status == "FIX_REQUESTED" and case.finding_count == 0:
                case.status = "RESOLVED"          # 수정요청 건 전부 해소 — 자동 종결
                resolved_cases += 1
            elif (auto_clear_on and case.status == "OPEN"
                  and case.finding_count == 0 and float(case.risk_amount or 0) == 0):
                # P1 자동 심사 — 무적발(리스크0) 케이스 자동 이상없음. 안전: 적발 있으면 절대 자동처리 안 함.
                case.status, case.auto_cleared = "CLEARED", True
                auto_cleared += 1
                await _audit.record_audit(session, actor=actor, action="AUTO_CLEAR",
                                          entity="claims.pre_review_cases",
                                          target_id=str(case.id),
                                          after={"status": "CLEARED", "auto": True})  # 가역·추적
            saved.append({"case_id": case.id, "chart_no": chart,
                          "status": case.status, "findings": case.finding_count})

        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.pre_review_run", target_id=str(len(saved)),
                                  after={"cases": len(saved)})
        return {"cases": saved, "auto_resolved": resolved_cases,
                "auto_cleared": auto_cleared,        # P1 자동 이상없음 건수
                "auto_dismissed": auto_dismissed}    # P2 자동 오탐 건수

    async def _refresh_rollup(self, session: AsyncSession, case: PreReviewCase, *,
                              claimed_amt: float | None = None, now=None,
                              findings: list | None = None) -> None:
        # findings 주입 시 재조회 생략(N+1 제거) — 호출부가 flush 후 현재 전량을 이미 보유한 경우.
        rows = findings if findings is not None else (await session.scalars(
            select(PreReviewFinding).where(PreReviewFinding.case_id == case.id))).all()
        live = [f for f in rows if not f.resolved and f.decision != "DISMISS"]
        case.finding_count = len(live)
        case.max_severity = ("BLOCK" if any(f.severity == "BLOCK" for f in live)
                             else "WARN" if live else None)
        # 위험액 — (코드, 진료일)별 노출 1회 집계: 같은 라인에 중복 룰 적발 시 인플레는 방지하되,
        # 동일 코드의 상이한 진료일 라인은 각각 합산(종전 코드 단일키는 다중라인 과소집계 — 계산검토
        # M-9). 동일 키 다중 룰은 최대 노출액 채택. 무코드 적발은 각각 독립 위험이라 개별 합산.
        by_key: dict[tuple, float] = {}
        no_code_sum = 0.0
        for f in live:
            if f.code:
                k = (f.code, f.treated_on)
                by_key[k] = max(by_key.get(k, 0.0), float(f.amount))
            else:
                no_code_sum += float(f.amount)
        case.risk_amount = sum(by_key.values()) + no_code_sum
        if claimed_amt is not None:
            case.claimed_amt = claimed_amt
        if now is not None:
            case.last_run_at = now

    async def list_cases(self, session: AsyncSession, *, status: str | None = None,
                         assignee_id: int | None = None, severity: str | None = None,
                         chart_no: str | None = None, encounter_type: str | None = None,
                         treated_on=None, care_month: str | None = None,
                         dept_codes: list[str] | None = None) -> list[dict]:
        q = select(PreReviewCase).order_by(PreReviewCase.risk_amount.desc(),
                                           PreReviewCase.id.desc())
        if dept_codes is not None:   # 부서 경계(IDOR) — 단건 접근과 일관(목록·반출도 본부서만)
            q = q.where(PreReviewCase.dept_code.in_(dept_codes))   # 빈 리스트=아무것도 안 보임(과차단 안전)
        if status:
            q = q.where(PreReviewCase.status == status)
        if care_month:
            q = q.where(PreReviewCase.care_month == care_month)
        if assignee_id:
            q = q.where(PreReviewCase.assignee_id == assignee_id)
        if severity:
            q = q.where(PreReviewCase.max_severity == severity)
        if chart_no:
            q = q.where(PreReviewCase.chart_no.ilike(f"%{chart_no}%"))
        if encounter_type:
            q = q.where(PreReviewCase.encounter_type == encounter_type)
        if treated_on:   # 진료일(일 단위) — 해당 진료일에 적발 있는 케이스만(treated_on은 #195부터 부착)
            q = q.where(PreReviewCase.id.in_(
                select(PreReviewFinding.case_id)
                .where(PreReviewFinding.treated_on == treated_on)))
        rows = (await session.scalars(q)).all()
        # 케이스별 미해소 적발 심각도 분해(BLOCK/WARN) — 목록 트리아지용(C)
        sev: dict[int, dict[str, int]] = {}
        ids = [c.id for c in rows]
        if ids:
            agg = await session.execute(
                select(PreReviewFinding.case_id, PreReviewFinding.severity, func.count())
                .where(PreReviewFinding.case_id.in_(ids),
                       PreReviewFinding.resolved.is_(False),
                       PreReviewFinding.decision != "DISMISS")
                .group_by(PreReviewFinding.case_id, PreReviewFinding.severity))
            for cid, s, n in agg.all():
                sev.setdefault(cid, {})[s] = n
        return [{**self._case_dto(c),
                 "block_count": sev.get(c.id, {}).get("BLOCK", 0),
                 "warn_count": sev.get(c.id, {}).get("WARN", 0)} for c in rows]

    @staticmethod
    def _case_dto(c: PreReviewCase) -> dict:
        return {"id": c.id, "chart_no": c.chart_no, "care_month": c.care_month,
                "dept_code": c.dept_code, "encounter_type": c.encounter_type,
                "status": c.status,
                "max_severity": c.max_severity, "risk_amount": float(c.risk_amount),
                "claimed_amt": float(c.claimed_amt), "finding_count": c.finding_count,
                "assignee_id": c.assignee_id, "auto_cleared": c.auto_cleared,
                "last_run_at": c.last_run_at}

    async def case_diagnoses(self, session: AsyncSession, *, chart_no: str,
                             care_month: str | None) -> list[str]:
        """케이스(차트×진료월) 상병코드(진단) 집계 — 의학적 심사 컨텍스트(미수신 시 빈 목록)."""
        from datetime import date as _dt
        from erp.patient.models import BillingLine
        dxg = (care_month or "").replace("-", "")
        if len(dxg) < 6:
            return []
        y, m = int(dxg[:4]), int(dxg[4:6])
        nx = _dt(y + (m == 12), (m % 12) + 1, 1)
        out: list[str] = []
        seen: set[str] = set()
        for dxs in (await session.scalars(select(BillingLine.diagnosis_codes).where(
                BillingLine.chart_no == chart_no, BillingLine.cancelled.is_(False),
                BillingLine.treated_on >= _dt(y, m, 1), BillingLine.treated_on < nx,
                BillingLine.diagnosis_codes.isnot(None)))).all():
            import re
            for d in re.split(r"[,;\s]+", str(dxs).strip()):   # 콤마·세미콜론·공백 다중 구분자 수용
                if d and d not in seen:
                    seen.add(d)
                    out.append(d)
        return out

    async def case_statement(self, session: AsyncSession, *, case_id: int) -> dict:
        """진료비 명세 (P1b) — 케이스 chart_no×care_month로 라인별 계산 분해
        + 라인↔적발 매핑 + codeType 그룹 소계. billing_engine 재사용(계산 SoT)."""
        from erp.patient.billing_service import BillingService
        case = await session.get(PreReviewCase, case_id)
        if case is None:
            raise ValidationError("존재하지 않는 케이스입니다")
        inv = await BillingService().invoice(
            session, chart_no=case.chart_no, care_month=case.care_month, interim=True)

        findings = (await session.scalars(
            select(PreReviewFinding).where(PreReviewFinding.case_id == case_id))).all()
        # 적발 부착: 날짜특정 적발은 (코드, 진료일)로 해당 라인에만, 진료일 없는 적발(기간룰·레거시
        # NULL)은 코드 기준 폴백 — 다른 진료일 라인에 엉뚱하게 표시되던 문제 해소.
        fmap_dated: dict = {}
        fmap_code: dict[str, list] = {}
        for f in findings:
            if not f.code:
                continue
            item = {"id": f.id, "rule_name": f.rule_name or f.rule_code,
                    "severity": f.severity, "detail": f.detail,
                    "reason_group": f.reason_group, "reason_code": f.reason_code,
                    "amount": float(f.amount), "decision": f.decision,
                    "resolved": f.resolved}
            if f.treated_on is not None:
                fmap_dated.setdefault((f.code, f.treated_on), []).append(item)
            elif not f.resolved:   # 진료일 없는 적발(기간룰)은 미해소만 코드 폴백 — 해소된 레거시 NULL 노이즈 제거
                fmap_code.setdefault(f.code, []).append(item)

        # 1차 그룹 = 임상영역(심사평가 스캔용: 진찰/처치·수술/검사/영상/투약…),
        # 급여분류(행위료/약제비/재료대 등) 소계는 별도 요약(benefit_summary)으로 분리.
        from erp.patient.billing_classify import (
            BENEFIT_ORDER, CLINICAL_ORDER, classify_benefit, classify_clinical,
            clinical_label, label, time_surcharge,
        )
        groups: dict[str, dict] = {}
        bsum: dict[str, dict] = {}
        for ln in inv.get("lines", []):
            ct, nm = ln.get("code_type"), ln.get("name")
            cat = classify_clinical(ct, nm)
            bc = ln.get("benefit_category") or classify_benefit(ct, nm)
            g = groups.setdefault(cat, {"category": cat, "label": clinical_label(cat),
                                        "lines": [], "claimed": 0, "patient": 0})
            _c = ln.get("code") or ""
            g["lines"].append({**ln, "category": cat, "benefit_category": bc,
                               "benefit_label": label(bc),
                               "time_surcharge": time_surcharge(ln.get("treated_at")),
                               "findings": (fmap_dated.get((_c, ln.get("treated_on")), [])
                                            + fmap_code.get(_c, []))})
            g["claimed"] += ln.get("total", 0)
            g["patient"] += ln.get("patient", 0)
            b = bsum.setdefault(bc, {"category": bc, "label": label(bc),
                                     "claimed": 0, "patient": 0})
            b["claimed"] += ln.get("total", 0)
            b["patient"] += ln.get("patient", 0)
        ordered = [groups[k] for k in CLINICAL_ORDER if k in groups]
        benefit_summary = [bsum[k] for k in BENEFIT_ORDER if k in bsum]

        # 종별가산 명시 — 진료월 시점 기관 종별(H2: 계산과 동일 시점, 소급 정합)
        from datetime import date as _date
        from erp.patient.billing_engine import GRADE_LABEL
        from erp.patient.billing_rates import load_billing_rates
        d = (case.care_month or "").replace("-", "")
        as_of = _date(int(d[:4]), int(d[4:6]), 1) if len(d) >= 6 else _date.today()
        gcfg = await load_billing_rates(session, as_of)
        grade_label = GRADE_LABEL.get(gcfg["grade"]) if gcfg["grade"] else None

        # 상병코드(진단) 집계 — 의학적 심사 컨텍스트(HIS 동봉분, 미수신 시 빈 목록)
        dx_codes = await self.case_diagnoses(
            session, chart_no=case.chart_no, care_month=case.care_month)
        return {
            "case_id": case.id, "chart_no": case.chart_no,
            "care_month": case.care_month, "encounter_type": case.encounter_type,
            "diagnosis_codes": dx_codes,          # 상병(진단) — 의학적 심사
            "institution_grade": grade_label,     # 종별가산 명시(단가 반영분)
            "summary": {"total": inv.get("total", 0),
                        "patient_share": inv.get("patient_share", 0),
                        "insurer_share": inv.get("insurer_share", 0),
                        "non_covered": inv.get("non_covered", 0),
                        "prepaid": inv.get("prepaid", 0),
                        "balance_due": inv.get("balance_due", 0)},
            "groups": ordered,                    # 1차 = 임상영역(심사 스캔)
            "benefit_summary": benefit_summary,   # 급여분류 소계(청구 관점) — 하단 요약
            "warnings": inv.get("warnings", []),
            "queries": await self.list_queries(session, case_id=case_id),
            "available": inv.get("available", True)}

    # ── 의료진 협업 루프 (P2 — 삭감 방지) ───────────────────
    async def create_query(self, session: AsyncSession, *, actor: int, case_id: int,
                           question: str, finding_id: int | None = None,
                           line_code: str | None = None,
                           assigned_to: int | None = None) -> PreReviewQuery:
        """의료진 질의 생성 — 적발(finding_id)/라인(line_code) 단위(둘 다 선택)."""
        case = await session.get(PreReviewCase, case_id)
        if case is None:
            raise ValidationError("존재하지 않는 케이스입니다")
        if not (question or "").strip():
            raise ValidationError("질의 내용은 필수입니다")
        if finding_id is not None:   # 적발-케이스 정합 — 타 케이스 적발을 질의로 엮어 종결·롤업 오염 차단
            f = await session.get(PreReviewFinding, finding_id)
            if f is None or f.case_id != case_id:
                raise ValidationError("적발이 해당 케이스에 속하지 않습니다")
        q = PreReviewQuery(case_id=case_id, finding_id=finding_id, line_code=line_code,
                           question=question.strip(), raised_by=actor,
                           assigned_to=assigned_to, status="OPEN")
        session.add(q)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.pre_review_queries", target_id=str(q.id),
                                  after={"case_id": case_id, "finding_id": finding_id})
        return q

    async def reply_query(self, session: AsyncSession, *, actor: int, query_id: int,
                          body: str, reply_type: str) -> PreReviewReply:
        """의료진 회신 — EVIDENCE(근거 제시) | REVISE(오더 수정 통보). 질의 상태 ANSWERED."""
        q = await session.get(PreReviewQuery, query_id)
        if q is None:
            raise ValidationError("존재하지 않는 질의입니다")
        if reply_type not in ("EVIDENCE", "REVISE"):
            raise ValidationError("회신 유형은 EVIDENCE 또는 REVISE")
        if not (body or "").strip():
            raise ValidationError("회신 내용은 필수입니다")
        r = PreReviewReply(query_id=query_id, reply_type=reply_type,
                           body=body.strip(), author=actor)
        session.add(r)
        if q.status == "OPEN":
            q.status = "ANSWERED"
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.pre_review_replies", target_id=str(r.id),
                                  after={"query_id": query_id, "reply_type": reply_type})
        return r

    async def resolve_query(self, session: AsyncSession, *, actor: int, query_id: int,
                            status: str, note: str | None = None,
                            confirmed_by_employee: int | None = None) -> dict:
        """질의 종결 — ACCEPTED(근거 인정→연결 적발 DISMISS) | REVISED(수정 완료).

        ACCEPTED + 연결 적발(finding_id) → 그 적발을 DISMISS(오탐, 사유=종결노트) 연계
        = 삭감 방지 루프 핵심(의료진 근거로 적발 해소).
        의료법: 의학판단 적발(physician_gated)의 종결은 책임 의료진 식별(confirmed_by_employee)과
        의료진 회신(EVIDENCE/REVISE)이 있어야만 가능 — 심사자 단독 종결 금지.
        """
        from erp.claims.models import PreReviewReply
        q = await session.get(PreReviewQuery, query_id)
        if q is None:
            raise ValidationError("존재하지 않는 질의입니다")
        if status not in ("ACCEPTED", "REVISED"):
            raise ValidationError("종결 상태는 ACCEPTED 또는 REVISED")
        f = await session.get(PreReviewFinding, q.finding_id) if q.finding_id else None
        gated = bool(f) and await self._is_physician_gated(session, f.rule_code)
        if gated:   # 의학판단 적발 — 의료진 확인 강제(실존 의료진 + 회신 존재)
            if not confirmed_by_employee:
                raise ValidationError("의학판단 적발 종결에는 확인 책임 의료진(confirmed_by_employee)이 필요합니다")
            # 백도어 차단 — 확인의가 실존 의료진(staff_class=MEDICAL)인지 검증. 임의 정수·비의료인
            # 심사자가 스스로를 확인의로 지정해 gated 적발을 자가 종결하던 우회 경로 차단.
            if not await self._is_medical_employee(session, confirmed_by_employee):
                raise ValidationError(
                    "확인 책임자는 실존 의료진(staff_class=MEDICAL)이어야 합니다 — 심사자 단독 종결 불가")
            has_reply = await session.scalar(
                select(PreReviewReply.id).where(PreReviewReply.query_id == query_id).limit(1))
            if not has_reply:
                raise ValidationError("의학판단 적발 종결에는 의료진 회신(근거/수정)이 선행되어야 합니다")
        q.status = status
        q.resolution_note = note
        if confirmed_by_employee:
            q.confirmed_by_employee = confirmed_by_employee
        q.resolved_at = datetime.now(timezone.utc)
        dismissed = None
        if status == "ACCEPTED" and f is not None and f.decision == "PENDING":
            f.decision = "DISMISS"
            who = f" · 확인의 {confirmed_by_employee}" if confirmed_by_employee else ""
            f.decision_note = f"의료진 근거 인정(질의#{query_id}{who}): {note or ''}"[:300]
            f.decided_by = actor
            f.decided_at = datetime.now(timezone.utc)   # decide_finding과 대칭(감사 정렬·조회 누락 방지)
            dismissed = f.id
            # 롤업은 적발이 속한 케이스(f.case_id) 기준 — 질의 케이스(q.case_id)와 어긋나도 정합 유지
            case = await session.get(PreReviewCase, f.case_id)
            if case is not None:
                await self._refresh_rollup(session, case, now=datetime.now(timezone.utc))
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.pre_review_queries", target_id=str(query_id),
                                  after={"status": status, "dismissed_finding": dismissed,
                                         "confirmed_by_employee": confirmed_by_employee})
        return {"query_id": query_id, "status": status, "dismissed_finding": dismissed}

    async def list_queries(self, session: AsyncSession, *, case_id: int | None = None,
                           assigned_to: int | None = None,
                           status: str | None = None,
                           dept_codes: list[str] | None = None) -> list[dict]:
        """질의 목록 — 케이스별(상세 타임라인)·의료진별(내 질의함)·전역(관리 로그).

        dept_codes 지정 시 본부서 케이스 질의만(전역 목록 IDOR 경계). 케이스 컨텍스트
        (chart_no·care_month) 동봉 — 관리 화면에서 어느 환자/월인지 식별.
        """
        qy = select(PreReviewQuery).order_by(PreReviewQuery.created_at.desc())
        if case_id is not None:
            qy = qy.where(PreReviewQuery.case_id == case_id)
        if assigned_to is not None:
            qy = qy.where(PreReviewQuery.assigned_to == assigned_to)
        if status:
            qy = qy.where(PreReviewQuery.status == status)
        if dept_codes is not None:                       # 부서 경계 — 전역 목록도 본부서만
            qy = qy.where(PreReviewQuery.case_id.in_(
                select(PreReviewCase.id).where(PreReviewCase.dept_code.in_(dept_codes))))
        rows = (await session.scalars(qy)).all()
        cases: dict[int, PreReviewCase] = {}
        reps_by_q: dict[int, list[PreReviewReply]] = {}
        if rows:
            cids = {q.case_id for q in rows}
            cases = {c.id: c for c in (await session.scalars(
                select(PreReviewCase).where(PreReviewCase.id.in_(cids)))).all()}
            for r in (await session.scalars(            # 답글 일괄 조회(전역 질의함 N+1 제거)
                select(PreReviewReply).where(PreReviewReply.query_id.in_([q.id for q in rows]))
                .order_by(PreReviewReply.id))).all():
                reps_by_q.setdefault(r.query_id, []).append(r)
        out = []
        for q in rows:
            replies = reps_by_q.get(q.id, [])
            cc = cases.get(q.case_id)
            out.append({
                "id": q.id, "case_id": q.case_id, "finding_id": q.finding_id,
                "chart_no": cc.chart_no if cc else None,
                "care_month": cc.care_month if cc else None,
                "line_code": q.line_code, "status": q.status, "question": q.question,
                "raised_by": q.raised_by, "assigned_to": q.assigned_to,
                "resolution_note": q.resolution_note, "created_at": q.created_at,
                "replies": [{"id": r.id, "reply_type": r.reply_type, "body": r.body,
                             "author": r.author, "created_at": r.created_at}
                            for r in replies]})
        return out

    async def adjustment_history(self, session: AsyncSession, *, chart_no: str) -> dict:
        """동일 차트(환자)의 과거 실삭감 이력 — 검토 우선순위 컨텍스트 (Tier2).

        EDIFACT 적재분(claim_statements × claim_adjustments) 집계.
        G1 통보서 본격 수신 시 이 집계가 룰 적중률 라벨로도 사용됨.
        """
        rows = (await session.execute(
            select(func.count(ClaimAdjustment.id),
                   func.coalesce(func.sum(ClaimAdjustment.adj_amt), 0))
            .join(ClaimStatement, ClaimStatement.id == ClaimAdjustment.statement_id)
            .where(ClaimStatement.chart_no == chart_no))).one()
        cats = (await session.execute(
            select(ClaimAdjustment.adj_category, func.count(ClaimAdjustment.id))
            .join(ClaimStatement, ClaimStatement.id == ClaimAdjustment.statement_id)
            .where(ClaimStatement.chart_no == chart_no)
            .group_by(ClaimAdjustment.adj_category)
            .order_by(func.count(ClaimAdjustment.id).desc()).limit(5))).all()
        return {"count": int(rows[0]), "total_amt": float(rows[1]),
                "top_categories": [{"category": c or "미분류", "count": int(n)}
                                   for c, n in cats]}

    async def auto_actions(self, session: AsyncSession, *,
                           dept_codes: list[str] | None = None, limit: int = 100) -> dict:
        """자동 처리 근거·로그 — 자동 이상없음 케이스 + 자동 오탐 적발(근거·시각·상태).

        심사자가 자동 판정을 검토·오버라이드(케이스 열어 상태/결정 변경)하기 위한 관리 뷰.
        재개된 케이스는 auto_cleared 해제되므로 '현재 자동상태'만 — 전체 이력은 감사로그(AUTO_*).
        """
        case_q = select(PreReviewCase).where(PreReviewCase.auto_cleared.is_(True))
        if dept_codes is not None:
            case_q = case_q.where(PreReviewCase.dept_code.in_(dept_codes))
        cases = (await session.scalars(
            case_q.order_by(PreReviewCase.last_run_at.desc()).limit(limit))).all()

        f_q = (select(PreReviewFinding, PreReviewCase)
               .join(PreReviewCase, PreReviewCase.id == PreReviewFinding.case_id)
               .where(PreReviewFinding.auto_decided.is_(True)))
        if dept_codes is not None:
            f_q = f_q.where(PreReviewCase.dept_code.in_(dept_codes))
        frows = (await session.execute(
            f_q.order_by(PreReviewFinding.decided_at.desc()).limit(limit))).all()

        return {
            "auto_cleared": [{"case_id": c.id, "chart_no": c.chart_no, "care_month": c.care_month,
                              "status": c.status, "claimed_amt": float(c.claimed_amt),
                              "last_run_at": c.last_run_at} for c in cases],
            "auto_dismissed": [{"finding_id": f.id, "case_id": c.id, "chart_no": c.chart_no,
                                "care_month": c.care_month, "rule_name": f.rule_name or f.rule_code,
                                "code": f.code, "amount": float(f.amount), "note": f.decision_note,
                                "decided_at": f.decided_at, "resolved": f.resolved}
                               for f, c in frows]}

    async def _auto_dismiss_patterns(self, session: AsyncSession, *,
                                     min_sample: int) -> set[tuple[str, str]]:
        """P2 확정 오탐 패턴 — (rule_code, code)가 **인간** 결정 min_sample건↑ & 전건 DISMISS.

        인간 결정만 집계(auto_decided 제외 = 자동→자동 피드백 루프 차단). ACCEPT 단 1건이라도
        있으면 제외(보수적 — 그 패턴은 실제 삭감일 수 있음). 환수 리스크 방지.

        ⚠ **physician_gated 룰 전면 제외**(의료법·#315): 의학판단 적발은 인간 DISMISS(의료진 확인
        경유)가 쌓여도 패턴화하지 않는다 — gated는 매 건 의료진 확인이 법적 요건이라 과거 확인 이력으로
        미래 적발을 자동 종결하면 확인 없이 오탐 처리가 되어 게이트 우회(CRITICAL). LEFT JOIN 후
        physician_gated=False(또는 룰 미등록)만 패턴 후보.
        """
        from erp.claims.models import PreReviewRule
        rows = (await session.execute(
            select(PreReviewFinding.rule_code, PreReviewFinding.code,
                   func.count(PreReviewFinding.id),
                   func.sum(sa_case((PreReviewFinding.decision == "ACCEPT", 1), else_=0)))
            .outerjoin(PreReviewRule, PreReviewRule.code == PreReviewFinding.rule_code)
            .where(PreReviewFinding.decision.in_(("DISMISS", "ACCEPT")),
                   PreReviewFinding.auto_decided.is_(False),     # 인간 결정만
                   PreReviewFinding.code.isnot(None),
                   func.coalesce(PreReviewRule.physician_gated, False).is_(False))  # gated 제외
            .group_by(PreReviewFinding.rule_code, PreReviewFinding.code))).all()
        return {(rc, code) for rc, code, n, acc in rows
                if int(n) >= min_sample and int(acc or 0) == 0}

    async def stats(self, session: AsyncSession, *, dept_codes: list[str] | None = None) -> dict:
        """룰별 적발·오탐률 + 상태 분포 + 위험액 합 — 통계 탭·룰 보정 근거.

        dept_codes 지정 시 본부서 케이스만 집계(부서 경계 — 목록·단건과 일관).
        """
        dept_cases = (select(PreReviewCase.id).where(PreReviewCase.dept_code.in_(dept_codes))
                      if dept_codes is not None else None)
        rule_q = (select(PreReviewFinding.rule_code, PreReviewFinding.rule_name,
                         func.count(PreReviewFinding.id),
                         func.sum(sa_case((PreReviewFinding.decision == "DISMISS", 1), else_=0)),
                         func.sum(sa_case((PreReviewFinding.decision == "ACCEPT", 1), else_=0)))
                  .group_by(PreReviewFinding.rule_code, PreReviewFinding.rule_name)
                  .order_by(func.count(PreReviewFinding.id).desc()))
        if dept_cases is not None:
            rule_q = rule_q.where(PreReviewFinding.case_id.in_(dept_cases))
        rule_rows = (await session.execute(rule_q)).all()
        by_rule = [{"rule_code": rc, "rule_name": rn, "findings": int(n),
                    "dismissed": int(d or 0), "accepted": int(a or 0),
                    "dismiss_rate": round((d or 0) / n, 3) if n else 0.0}
                   for rc, rn, n, d, a in rule_rows]
        status_q = (select(PreReviewCase.status, func.count(PreReviewCase.id),
                           func.coalesce(func.sum(PreReviewCase.risk_amount), 0))
                    .group_by(PreReviewCase.status))
        if dept_codes is not None:
            status_q = status_q.where(PreReviewCase.dept_code.in_(dept_codes))
        status_rows = (await session.execute(status_q)).all()
        # 거부사유 대분류 분포(P0-2) — 사유코드 기반 통계
        from erp.claims.pre_review import REASON_GROUP_LABEL
        reason_q = (select(PreReviewFinding.reason_group, func.count(PreReviewFinding.id),
                           func.coalesce(func.sum(PreReviewFinding.amount), 0))
                    .where(PreReviewFinding.reason_group.is_not(None))
                    .group_by(PreReviewFinding.reason_group)
                    .order_by(func.count(PreReviewFinding.id).desc()))
        if dept_cases is not None:
            reason_q = reason_q.where(PreReviewFinding.case_id.in_(dept_cases))
        reason_rows = (await session.execute(reason_q)).all()
        return {"by_rule": by_rule,
                "by_status": [{"status": s, "count": int(n), "risk_amount": float(r)}
                              for s, n, r in status_rows],
                "by_reason_group": [{"group": g, "label": REASON_GROUP_LABEL.get(g, g),
                                     "findings": int(n), "amount": float(amt)}
                                    for g, n, amt in reason_rows]}

    async def triage(self, session: AsyncSession, *, dept_codes: list[str] | None = None,
                     limit: int = 50) -> dict:
        """삭감위험 triage(P2-a) — 확정 결정 이력의 패턴별 인정률로 미결 케이스 우선순위화.

        예측삭감 = Σ(미해소 적발 amount × 인정률[(rule_code, code)]). 이력 없는 패턴은 prior 0.5.
        휴먼인더루프(우선순위 제시일 뿐 자동결정 아님)·설명가능(패턴 인정률 기반). ML(XGBoost)은
        학습사례 충분 누적 후 P2-b.
        """
        rate_rows = (await session.execute(
            select(PreReviewFinding.rule_code, PreReviewFinding.code,
                   func.sum(sa_case((PreReviewFinding.decision == "ACCEPT", 1), else_=0)),
                   func.sum(sa_case((PreReviewFinding.decision == "DISMISS", 1), else_=0)))
            .where(PreReviewFinding.decision.in_(["ACCEPT", "DISMISS"]),
                   PreReviewFinding.auto_decided.is_(False))
            .group_by(PreReviewFinding.rule_code, PreReviewFinding.code))).all()
        rate = {}
        for rc, code, acc, dis in rate_rows:
            tot = (acc or 0) + (dis or 0)
            if tot:
                rate[(rc, code)] = (acc or 0) / tot
        cq = select(PreReviewCase).where(PreReviewCase.status.in_(
            ["OPEN", "IN_REVIEW", "BLOCKED", "FIX_REQUESTED"]))
        if dept_codes is not None:
            cq = cq.where(PreReviewCase.dept_code.in_(dept_codes))
        cases = (await session.scalars(cq)).all()
        if not cases:
            return {"items": []}
        fmap: dict = {}
        for f in (await session.scalars(select(PreReviewFinding).where(
                PreReviewFinding.case_id.in_([c.id for c in cases]),
                PreReviewFinding.resolved.is_(False),
                PreReviewFinding.decision != "DISMISS"))).all():
            fmap.setdefault(f.case_id, []).append(f)
        out = []
        for c in cases:
            fs = fmap.get(c.id, [])
            if not fs:
                continue
            score = sum(float(f.amount) * rate.get((f.rule_code, f.code), 0.5) for f in fs)
            out.append({"case_id": c.id, "chart_no": c.chart_no, "care_month": c.care_month,
                        "status": c.status, "risk_amount": float(c.risk_amount),
                        "predicted_reduction": round(score), "findings": len(fs),
                        "max_severity": c.max_severity})
        out.sort(key=lambda x: -x["predicted_reduction"])
        return {"items": out[:limit]}

    async def bulk_status(self, session: AsyncSession, *, actor: int,
                          items: list[dict], dept_codes: list[str] | None = None) -> dict:
        """시트 왕복 일괄 상태 반영 — (chart_no, care_month) 키 매칭, 전이규칙 위반은 스킵.

        dept_codes 지정 시 본부서 케이스만 반영(부서 경계 — 일괄경로 IDOR 차단, 단건과 일관).
        """
        if len(items) > 5000:                    # M-2: 일괄 입력 상한(DoS·행당 조회 폭주 방지)
            raise ValidationError(f"일괄 반영 상한 5,000행 초과({len(items)}행) — 분할 업로드하세요")
        # N+1 제거 — 입력 (chart_no, care_month) 키를 한 번에 로드(행당 SELECT → 1 일괄조회)
        keys = {(it.get("chart_no"), it.get("care_month")) for it in items}
        keys.discard((None, None))
        case_by_key: dict[tuple, PreReviewCase] = {}
        if keys:
            for c in (await session.scalars(select(PreReviewCase).where(
                    tuple_(PreReviewCase.chart_no, PreReviewCase.care_month).in_(keys)))).all():
                case_by_key[(c.chart_no, c.care_month)] = c
        applied, skipped = 0, []
        for it in items:
            chart, month, status = it.get("chart_no"), it.get("care_month"), it.get("status")
            case = case_by_key.get((chart, month))
            if case is None:
                skipped.append({"chart_no": chart, "reason": "케이스 없음"})
                continue
            if dept_codes is not None and case.dept_code not in dept_codes:   # 부서 경계(IDOR)
                skipped.append({"chart_no": chart, "reason": "타 부서 케이스"})
                continue
            if not status or status == case.status:
                continue
            try:
                await self.update_case(session, actor=actor, case_id=case.id, status=status)
                applied += 1
            except ValidationError as exc:
                skipped.append({"chart_no": chart, "reason": str(exc)})
        return {"applied": applied, "skipped": skipped}

    async def sync_from_billing_lines(self, session: AsyncSession, *, actor: int,
                                      care_month: str, chart_no: str | None = None) -> dict:
        """진료라인 미러(D-6) → 사전심사 자동 적재 — 심사팀 붙여넣기 없이 워크리스트 채움.

        단가: ERP 마스터(>0) 우선 → HIS 산정가 폴백(invoice와 동일 규칙).
        save_run 재사용 — 케이스 멱등·해소 마킹·재개가 그대로 적용.
        chart_no 지정 시 그 차트×월만 표적 재평가(이벤트 기반 실시간 — 룰은 차트의 월 전체 평가).
        """
        from datetime import date as _date

        from erp.claims.models import PreReviewRule
        from erp.claims.pre_review import PreReviewEngine
        from erp.patient.models import BillingLine

        digits = care_month.replace("-", "")
        y, m = int(digits[:4]), int(digits[4:6])
        nxt = _date(y + (m == 12), (m % 12) + 1, 1)
        cond = [BillingLine.cancelled.is_(False),
                BillingLine.treated_on >= _date(y, m, 1),
                BillingLine.treated_on < nxt]
        if chart_no is not None:                       # 표적(이벤트 기반) — 해당 차트만
            cond.append(BillingLine.chart_no == chart_no)
        rows = (await session.scalars(select(BillingLine).where(*cond))).all()
        if not rows:
            return {"care_month": care_month, "lines": 0, "cases": []}

        as_of = max(r.treated_on for r in rows if r.treated_on)
        from erp.patient.billing_rates import load_billing_rates
        erp_grade = (await load_billing_rates(session, as_of)).get("grade")  # 종별가산·정합 공용
        # 약품 매핑(CONFIRMED) 적용 fee — 미등재 오탐 방지. 종별가산(G6)도 fee 단가에 반영.
        # **진료일별** 조회(2026-10-09 검토 B) — 종전엔 월 전체를 마지막 진료일 하나로 평가해, 월중 단가
        # 변경이 이전 진료일에도 신단가로 적용되고(금액 이상 오탐) 월중 종료 코드는 UNKNOWN BLOCK 오탐이
        # 돼 케이스 종결을 막았다. 엔진용 fee는 코드별로 그 코드가 쓰인 가장 늦은 진료일 값을 합친다.
        by_day: dict = {}
        for r in rows:
            by_day.setdefault(r.treated_on or as_of, set()).add(r.code)
        fee_by_day = {d: await resolve_fee_with_mapping(session, codes, d, grade=erp_grade)
                      for d, codes in by_day.items()}
        fee: dict = {}
        for d in sorted(fee_by_day):
            for code, v in fee_by_day[d].items():
                if code in by_day[d]:
                    fee[code] = v

        lines = []
        for r in rows:
            # 라인 금액도 엔진과 **같은 fee**(코드별 최근 진료일 값)로 — 라인만 그날 단가로 두면 월중 단가
            # 변경 시 이전 라인이 금액이상으로 새로 걸렸다(2차 검토 B). 진료일별 조회의 목적은 월중 종료
            # 코드의 UNKNOWN 오탐 제거(멤버십)다.
            master = (fee.get(r.code) or {}).get("price", 0.0)
            his = float(r.unit_price_his) if r.unit_price_his is not None else 0.0
            # 비급여는 기관 자율가(HIS) 우선 — 매핑 보험약가로 덮어쓰지 않음(인보이스 표시 일관)
            if (r.coverage or "").upper() == "NONCOVERED":
                unit = his if his > 0 else master
            else:
                unit = master if master > 0 else his
            lines.append({"chart_no": r.chart_no, "code": r.code, "code_type": r.code_type,
                          "count": int(r.count), "amount": unit * float(r.count) or None,
                          "coverage": r.coverage, "dept_code": r.dept_code,
                          "eligibility_status": r.eligibility_status,
                          "treated_on": r.treated_on, "treated_at": r.treated_at,
                          "age": r.patient_age, "sex": r.patient_sex,
                          "diagnosis_codes": r.diagnosis_codes,   # 상병-수가 연계(G2)
                          "ingredient": r.ingredient_code,        # HIS 주성분(DUR 직접·#72)
                          "pregnancy_status": r.pregnancy_status,  # 임부금기 DUR(#72)
                          "institution_grade": r.institution_grade,  # HIS 종별(정합 점검 G6·#75)
                          "encounter_type": r.encounter_type})
        rules = (await session.scalars(
            select(PreReviewRule).where(
                PreReviewRule.active.is_(True),
                # 룰 유효기간은 종전대로 마지막 진료일 기준 — 기간 겹침으로 바꾸면 월중 종료 BLOCK 룰이 종료
                # 뒤 라인에도 적용돼 종결을 막는 회귀가 생긴다(2차 검토 B). 라인별 룰 적용은 엔진 개편 과제.
                or_(PreReviewRule.valid_from.is_(None), PreReviewRule.valid_from <= as_of),
                or_(PreReviewRule.valid_to.is_(None), PreReviewRule.valid_to >= as_of)))).all()
        engine = PreReviewEngine()
        # 공휴가산 판정에 DB 공휴 캘린더 주입(M-8) — fee 채널로 전달(_surcharge_check 소비)
        from erp.common.holidays import HolidayService
        fee["_extra_holidays"] = await HolidayService().dates(session)
        findings = engine.evaluate(lines=lines, rules=rules, fee=fee)
        # 약제 DUR(P0-1) — 주성분: HIS 동봉(ingredient_code) 우선, 미동봉분만 매핑 폴백
        from erp.claims.models import DrugIngredientMap, DurRule
        drug_lines = [ln for ln in lines if (ln.get("code_type") or "").upper() == "DRUG"]
        need_map = {ln.get("code") for ln in drug_lines if not ln.get("ingredient") and ln.get("code")}
        if need_map:
            ing_map = {m.insurance_code: m.ingredient_code for m in (await session.scalars(
                select(DrugIngredientMap).where(
                    DrugIngredientMap.insurance_code.in_(need_map)))).all()}
            for ln in drug_lines:
                if not ln.get("ingredient"):
                    ln["ingredient"] = ing_map.get(ln.get("code"))
        if any(ln.get("ingredient") for ln in drug_lines):
            dur_rules = (await session.scalars(
                select(DurRule).where(DurRule.active.is_(True)))).all()
            if dur_rules:
                findings = findings + engine.evaluate_dur(lines=lines, dur_rules=dur_rules)
        # 진단(KCD)-수가 연계(P1 G2) — 수가 필수상병 미기재 형식 점검
        from erp.claims.models import BundlingRule, DxProcedureRule
        dx_rules = (await session.scalars(
            select(DxProcedureRule).where(DxProcedureRule.active.is_(True)))).all()
        if dx_rules:
            findings = findings + engine.evaluate_dx(lines=lines, dx_rules=dx_rules)
        # 묶음청구(P1 G1/G4) — 패키지 수가 + 세부/재료 별도청구 적발
        bundle_rules = (await session.scalars(
            select(BundlingRule).where(BundlingRule.active.is_(True)))).all()
        if bundle_rules:
            findings = findings + engine.evaluate_bundle(lines=lines, bundle_rules=bundle_rules)
        # 종별 정합(P1 G6) — HIS 동봉 종별 vs ERP 마스터 불일치 점검(실 불일치 시에만 발화)
        findings = findings + engine.evaluate_grade(lines=lines, grade=erp_grade)
        out = await self.save_run(session, actor=actor, lines=lines, findings=findings)
        return {"care_month": care_month, "lines": len(lines),
                "findings": len(findings), **out}

    async def detail(self, session: AsyncSession, *, case_id: int) -> dict:
        case = await session.get(PreReviewCase, case_id)
        if case is None:
            raise ValidationError("존재하지 않는 케이스입니다")
        findings = (await session.scalars(
            select(PreReviewFinding).where(PreReviewFinding.case_id == case_id)
            .order_by(PreReviewFinding.resolved, PreReviewFinding.id))).all()
        from erp.claims.models import PreReviewRule
        gated = set((await session.scalars(
            select(PreReviewRule.code).where(PreReviewRule.physician_gated.is_(True)))).all())
        comments = (await session.scalars(
            select(PreReviewComment).where(PreReviewComment.case_id == case_id)
            .order_by(PreReviewComment.id))).all()
        return {**self._case_dto(case),
                "adjustment_history": await self.adjustment_history(
                    session, chart_no=case.chart_no),
                "findings": [{"id": f.id, "rule_code": f.rule_code, "rule_name": f.rule_name,
                              "severity": f.severity, "code": f.code, "treated_on": f.treated_on,
                              "reason_group": f.reason_group, "reason_code": f.reason_code,
                              "detail": f.detail, "amount": float(f.amount), "decision": f.decision,
                              "decision_note": f.decision_note, "resolved": f.resolved,
                              "auto_decided": f.auto_decided,
                              "physician_gated": f.rule_code in gated}  # 의학판단 — 단독오탐 불가
                             for f in findings],
                "comments": [{"id": m.id, "author": m.author, "body": m.body,
                              "created_at": m.created_at} for m in comments]}

    async def update_case(self, session: AsyncSession, *, actor: int, case_id: int,
                          status: str | None = None,
                          assignee_id: int | None = None) -> PreReviewCase:
        case = await session.get(PreReviewCase, case_id)
        if case is None:
            raise ValidationError("존재하지 않는 케이스입니다")
        before = {"status": case.status, "assignee_id": case.assignee_id}
        if status and status != case.status:
            if status not in TRANSITIONS.get(case.status, set()):
                raise ValidationError(f"허용되지 않는 상태 전이: {case.status} → {status}")
            # 안전경계(수동·자동 대칭) — 미해소 BLOCK 적발이 있는 케이스는 종결 불가.
            # finding_count/max_severity는 롤업 소스오브트루스(추가 조회 없음 — bulk 5,000행 N+1 회피).
            if (status in _TERMINAL_CLOSE and case.finding_count > 0
                    and case.max_severity == "BLOCK"):
                raise ValidationError(
                    "미해소 BLOCK(삭감위험) 적발이 있는 케이스는 종결(CLEARED/RESOLVED) 불가 — "
                    "해당 적발을 먼저 해소(수정)하거나 오탐 처리(사유 기재) 후 종결하세요")
            # 의료법 게이트 — 의학판단 적발(physician_gated)은 등급이 WARN이어도 의료진 확인 없이 케이스
            # 종결로 덮을 수 없다. 종전엔 BLOCK만 막아, 단독 DISMISS는 막힌 gated 적발을 케이스 CLEARED로
            # 우회할 수 있었다(2026-10-09 검토 B).
            if status in _TERMINAL_CLOSE and case.finding_count > 0:
                gated_open = await session.scalar(
                    select(func.count()).select_from(PreReviewFinding)
                    .join(PreReviewRule, PreReviewRule.code == PreReviewFinding.rule_code)
                    .where(PreReviewFinding.case_id == case.id,
                           PreReviewFinding.resolved.is_(False),
                           PreReviewFinding.decision != "DISMISS",   # 의료진 확인으로 오탐 처리된 건 제외
                           PreReviewRule.physician_gated.is_(True)))
                if gated_open:
                    raise ValidationError(
                        f"의료진 확인이 필요한 의학판단 적발 {gated_open}건이 미해소 — 해당 적발을 의료진 "
                        "확인으로 처리한 뒤 종결하세요(의료법)")
            case.status = status
            if status == "IN_REVIEW" and assignee_id is None and case.assignee_id is None:
                case.assignee_id = actor          # 검토 시작 시 미배정이면 본인 배정
        if assignee_id is not None:
            case.assignee_id = assignee_id
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.pre_review_cases", target_id=str(case_id),
                                  before=before,
                                  after={"status": case.status, "assignee_id": case.assignee_id})
        return case

    async def _is_physician_gated(self, session: AsyncSession, rule_code: str) -> bool:
        """룰이 의학판단 필요(physician_gated)인가 — 심사자 단독 오탐 차단 판단."""
        from erp.claims.models import PreReviewRule
        return bool(await session.scalar(
            select(PreReviewRule.physician_gated).where(PreReviewRule.code == rule_code)))

    async def _is_medical_employee(self, session: AsyncSession, emp_id: int) -> bool:
        """확인 책임자가 실존 의료진(staff_class=MEDICAL)인가 — 의학판단 적발 종결 게이트 검증.

        임의 정수·비의료인(심사자 본인 등)이 확인의로 참칭해 gated 적발을 자가 종결하는 백도어 차단
        (decide_finding의 단독 DISMISS 금지와 대칭). MedicalContractService와 동일 기준.
        """
        from erp.hr.models import Employee
        return (await session.scalar(
            select(Employee.staff_class).where(Employee.id == emp_id))) == "MEDICAL"

    async def decide_finding(self, session: AsyncSession, *, actor: int, finding_id: int,
                             decision: str, note: str | None = None) -> PreReviewFinding:
        if decision not in ("ACCEPT", "DISMISS"):
            raise ValidationError("decision은 ACCEPT|DISMISS")
        note = (note or "").strip()[:300] or None    # 정규화·절단(질의 연계 경로와 대칭, 로그 비대화 방지)
        if decision == "DISMISS" and not note:
            raise ValidationError("오탐(DISMISS) 처리에는 사유가 필요합니다")  # 캘리브레이션 데이터
        f = await session.get(PreReviewFinding, finding_id)
        if f is None:
            raise ValidationError("존재하지 않는 적발 항목입니다")
        # 의료법 — 의학판단 적발은 비의료인 심사자 단독 오탐 금지(의료진 질의·확인 경유로만 해소)
        if decision == "DISMISS" and await self._is_physician_gated(session, f.rule_code):
            raise ValidationError(
                "의학판단 적발은 단독 오탐 처리 불가 — 의료진 질의(권고·의견 전달) 후 "
                "의료진 근거·확인으로 종결하세요")
        # 재결정 감사추적 — 기존 결정(다른 심사자/자동)이 있으면 before 값 기록(직무분리·조용한 덮어쓰기
        # 가시화). 자동 DISMISS(auto_decided) 위에 수동 재결정 시 auto_decided 해제.
        before = ({"decision": f.decision, "decided_by": f.decided_by,
                   "auto_decided": f.auto_decided} if f.decision else None)
        f.decision, f.decision_note = decision, note
        f.decided_by, f.decided_at = actor, datetime.now(timezone.utc)
        f.auto_decided = False                            # 수동 결정 — 자동 표시 해제
        case = await session.get(PreReviewCase, f.case_id)
        if case is not None:                              # 고아 적발 방어(AttributeError 방지)
            await self._refresh_rollup(session, case)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.pre_review_findings",
                                  target_id=str(finding_id), before=before,
                                  after={"decision": decision, "note": note})
        return f

    async def add_comment(self, session: AsyncSession, *, actor: int, case_id: int,
                          body: str) -> PreReviewComment:
        if not (body or "").strip():
            raise ValidationError("코멘트 내용이 비어 있습니다")
        if await session.get(PreReviewCase, case_id) is None:
            raise ValidationError("존재하지 않는 케이스입니다")
        m = PreReviewComment(case_id=case_id, author=actor, body=body.strip())
        session.add(m)
        await session.flush()
        return m
