"""사전심사 룰엔진 (FR-41) — 청구 라인을 룰로 평가해 삭감 위험 사전 적발.

순수 평가 함수(DB 비의존) — 라우터가 활성 룰·수가맵을 주입. 고시 기반 세부룰은 협의(D41) 후
룰 테이블에 추가하면 엔진 수정 없이 확장. 샘플 4종(중복·산정횟수·미등재·금액이상).
"""
import logging
from dataclasses import dataclass

logger = logging.getLogger("erp.claims.pre_review")


@dataclass
class Finding:
    rule_code: str
    rule_name: str
    severity: str
    chart_no: str
    code: str
    detail: str
    treated_on: object = None   # 진료일 — 날짜특정 적발의 명세 라인 정확 부착용(기간룰은 None)
    reason_group: str = ""      # 거부사유 대분류(A 산정착오·B 급여/산정기준초과·D 중복·K 코드착오·M 의학적타당성)
    reason_code: str = ""       # 거부사유 세분코드(예: B-CNT) — 설명가능성·통계·학습 피처


# 룰타입 → (거부사유 대분류, 세분코드). HIRA 청구오류 유형 + 내부 세분(CARC/RARC식 구조화).
# A=산정착오 · B=급여/산정기준 초과 · D=중복청구 · K=코드착오/누락 · M=의학적타당성(의료진 게이트)
REASON_BY_TYPE = {
    "DUPLICATE": ("D", "D-DUP"), "COUNT_LIMIT": ("B", "B-CNT"),
    "PERIOD_LIMIT": ("B", "B-PERIOD"), "SURCHARGE_CHECK": ("A", "A-SUR"),
    "LOS_RATIO": ("B", "B-LOS"), "AMOUNT_OUTLIER": ("A", "A-AMT"),
    "STAT_OUTLIER": ("A", "A-STAT"), "UNKNOWN_CODE": ("K", "K-UNK"),
    "EXCLUSIVE_PAIR": ("B", "B-EXCL"), "AGE_SEX_LIMIT": ("B", "B-AGESEX"),
    "NONCOVERED_CLAIM": ("K", "K-NONCOV"), "ELIGIBILITY_INVALID": ("B", "B-ELIG"),
    # 향후(P0-1/P1): DUR·진단연계
    "DRUG_CONTRA": ("B", "B-DUR"), "DRUG_DUP_THERAPY": ("D", "D-DRUG"),
    "DIAGNOSIS_MISMATCH": ("M", "M-DX"), "BUNDLE_VIOLATION": ("B", "B-BUNDLE"),
    "MATERIAL_UNBUNDLING": ("B", "B-MATERIAL"),
}
REASON_GROUP_LABEL = {
    "A": "산정착오", "B": "급여·산정기준 초과", "D": "중복청구",
    "K": "코드착오·누락", "M": "의학적 타당성",
}


def _duplicate(lines, rule, fee):
    seen, out = set(), []
    for ln in lines:
        key = (ln.get("chart_no"), ln.get("code"), str(ln.get("treated_on")))
        if key in seen:
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               ln.get("code") or "",
                               f"동일 진료일({ln.get('treated_on')}) {ln.get('code')} 중복청구",
                               ln.get("treated_on")))
        seen.add(key)
    return out


def _is_drug_material(ln) -> bool:
    """약제·재료 — count가 투여량(정·바이알·개수)이라 일반 행위 산정횟수 룰 대상 아님."""
    return (ln.get("code_type") or "TARIFF").upper() in ("DRUG", "MATERIAL")


_MAI_LABEL = {1: "라인단위 MAI1", 2: "일자절대 MAI2", 3: "임상벤치마크 MAI3"}


def _norm_count(ln) -> int:
    """산정횟수 정규화 — 미지정(None)만 1로 보정. 0·음수(취소·조정 라인)는 그대로 둬서
    `or 1`이 0건을 1로 오탐하거나(거짓 적발) 음수가 합산을 깎는 일을 막는다."""
    c = ln.get("count")
    return 1 if c is None else c


def _count_limit(lines, rule, fee):
    """산정횟수 상한 — HIRA 「최대산정횟수」·NCCI MUE 정합. params.mai로 단위 구분.

    mai=1 라인단위(개별 라인 count 초과) · mai∈(2,3,None) 일자합산(동일 진료일 누적).
    심각도는 rule.severity(DB) — 일자절대(MAI2)는 BLOCK, 벤치마크(MAI3)는 WARN으로 운영.
    """
    p = rule.params or {}
    mx = p.get("max", 1)
    suffix = f", {_MAI_LABEL[p['mai']]}" if p.get("mai") in _MAI_LABEL else ""
    if p.get("mai") == 1:             # 라인단위 — 개별 라인의 count 초과
        return [Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                        ln.get("code") or "",
                        f"{ln.get('code')} 산정 {_norm_count(ln)}회 (상한 {mx}{suffix})",
                        ln.get("treated_on"))
                for ln in lines if not _is_drug_material(ln) and _norm_count(ln) > mx]
    agg = {}
    for ln in lines:
        if _is_drug_material(ln):     # 약제·재료 투여량은 행위 산정횟수 대상 아님(오탐 방지)
            continue
        c = _norm_count(ln)
        if c <= 0:                    # 0건·음수(취소·조정)는 산정횟수 합산 대상 아님
            continue
        key = (ln.get("chart_no"), ln.get("code"), ln.get("treated_on"))
        agg[key] = agg.get(key, 0) + c
    return [Finding(rule.code, rule.name, rule.severity, chart or "", code or "",
                    f"{code} 산정 {cnt}회 (상한 {mx}{suffix})", day)
            for (chart, code, day), cnt in agg.items() if cnt > mx]


def _unknown_code(lines, rule, fee):
    # detail은 rule.name("수가 마스터 미등재 코드")과 중복 안 되게 코드만(표시: "{rule_name}: {detail}")
    return [Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                    ln.get("code") or "", f"청구 코드 {ln.get('code')}", ln.get("treated_on"))
            for ln in lines if ln.get("code") and ln.get("code") not in fee]


# fee 값: float(레거시·단가) 또는 dict{price, cap, type} — 헬퍼로 양쪽 수용(하위호환)
def _price(fee, code):
    v = fee.get(code)
    return v.get("price") if isinstance(v, dict) else v


def _cap(fee, code):
    v = fee.get(code)
    return v.get("cap") if isinstance(v, dict) else None


def _ctype(fee, code):
    v = fee.get(code)
    return v.get("type") if isinstance(v, dict) else "TARIFF"


def _amount_outlier(lines, rule, fee):
    tol = (rule.params or {}).get("tolerance", 0.1)
    out = []
    for ln in lines:
        code, amt = ln.get("code"), ln.get("amount")
        if amt is None or code not in fee:
            continue
        cnt = _norm_count(ln)         # 취소(0)·조정(음수) 라인 제외 — 정규화 룰과 동형
        if cnt <= 0:                  # 음수 count가 상한/편차 부호를 뒤집어 오탐/미탐하는 것 방지
            continue
        if _ctype(fee, code) == "DRUG":          # 약가: 상한금액 초과 검증
            cap = _cap(fee, code)
            if cap is not None and amt > cap * cnt * 1.001:
                out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                                   code, f"청구액 {amt:,.0f} > 약가 상한 {cap * cnt:,.0f} 초과",
                                   ln.get("treated_on")))
            continue
        unit = _price(fee, code)                 # 수가/재료: 표준단가 대비 이상
        if unit is None:
            continue
        expected = unit * cnt
        # 상방 편차만 적발 — 과소청구(amt<expected)는 삭감 리스크 아닌 자기손해라 심사 대상 아님
        # (DRUG 분기의 상한초과-only와 방향 일관). 과대청구만 삭감·환수 리스크.
        if expected and (amt - expected) / expected > tol:
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               code, f"청구액 {amt:,.0f} > 표준 {expected:,.0f} (허용 {tol:.0%} 초과)",
                               ln.get("treated_on")))
    return out


# ── Tier 1 정형 룰 (PR-A) — 라인 선택 필드: age, sex, treated_at(ISO datetime) ──
def _age_sex_limit(lines, rule, fee):
    """수가·약가의 연령/성별 적용 제한.

    params.code 지정 = 해당 코드만(레거시). 미지정 = **fee_master 기반 자동**(G5) — 전 라인의
    fee[code].sex/min_age/max_age로 일괄 점검(코드별 1룰 불필요).
    """
    p = rule.params or {}
    target = p.get("code")
    out = []

    def _check(ln, sx, mn, mx):
        code = ln.get("code") or ""
        age, sex = ln.get("age"), (ln.get("sex") or "").upper()
        if sx and sex and sex != str(sx).upper():
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               code, f"성별 제한({sx}) 위반 — 환자 {sex}", ln.get("treated_on")))
        if age is not None and mn is not None and age < mn:
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               code, f"연령 하한({mn}세) 미달 — 환자 {age}세", ln.get("treated_on")))
        if age is not None and mx is not None and age > mx:
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               code, f"연령 상한({mx}세) 초과 — 환자 {age}세", ln.get("treated_on")))

    if target:                       # 레거시 — params 지정 단일 코드
        for ln in lines:
            if ln.get("code") == target:
                _check(ln, p.get("sex"), p.get("min_age"), p.get("max_age"))
        return out
    for ln in lines:                 # 자동 — fee_master 제약(있는 코드만)
        fm = fee.get(ln.get("code") or "")
        if not isinstance(fm, dict):
            continue
        sx, mn, mx = fm.get("sex"), fm.get("min_age"), fm.get("max_age")
        if sx or mn is not None or mx is not None:
            _check(ln, sx, mn, mx)
    return out


def _exclusive_pair(lines, rule, fee):
    """동시산정 불가 조합 — 같은 차트·진료일에 params.codes가 모두 청구되면 적발."""
    codes = set((rule.params or {}).get("codes") or [])
    if len(codes) < 2:
        return []
    by_day: dict = {}
    for ln in lines:
        if ln.get("code") in codes:
            by_day.setdefault((ln.get("chart_no"), ln.get("treated_on")), set()).add(ln["code"])
    return [Finding(rule.code, rule.name, rule.severity, chart or "",
                    "+".join(sorted(found)),
                    f"동시산정 불가 조합 동일 진료일({day}) 청구: {', '.join(sorted(found))}", day)
            for (chart, day), found in by_day.items() if codes <= found]


def _period_limit(lines, rule, fee):
    """주/월 단위 산정 상한 — params {max, window: 'W'|'M', code?(미지정 시 전 코드)}."""
    p = rule.params or {}
    mx, window, target = p.get("max", 1), (p.get("window") or "M").upper(), p.get("code")
    agg: dict = {}
    for ln in lines:
        code, d = ln.get("code"), ln.get("treated_on")
        if not code or d is None or (target and code != target):
            continue
        if not target and _is_drug_material(ln):   # 타깃 없는 일반 행위 산정만 — 약제·재료 제외
            continue
        c = _norm_count(ln)      # 취소(0)·조정(음수) 제외 — `or 1` 오탐/음수 상쇄 미탐 방지(_count_limit 동형)
        if c <= 0:
            continue
        period = (f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}" if window == "W"
                  else f"{d.year:04d}-{d.month:02d}")
        key = (ln.get("chart_no"), code, period)
        agg[key] = agg.get(key, 0) + c
    unit = "주" if window == "W" else "월"
    return [Finding(rule.code, rule.name, rule.severity, chart or "", code or "",
                    f"{code} {unit} 산정 {cnt}회 (상한 {mx}) — {period}")
            for (chart, code, period), cnt in agg.items() if cnt > mx]


def _to_kst(at):
    """진료시각을 KST 로컬로 환산 — tz-aware(UTC 저장·asyncpg 반환)면 Asia/Seoul로 변환.

    naive(테스트·미상)는 그대로 둠(로컬 의미로 간주). 미변환 시 UTC 시각·날짜로 야간창·공휴
    판정이 systematically 어긋나 정당한 야간가산을 오탐하던 결함(HIGH) 해소.
    """
    if getattr(at, "tzinfo", None) is None:
        return at
    from zoneinfo import ZoneInfo
    return at.astimezone(ZoneInfo("Asia/Seoul"))


def _is_surcharge_holiday(d, extra_holidays=None) -> bool:
    """의료 공휴일가산 대상일 — 일요일 + 법정공휴일(고정·연도별·DB 캘린더). 토요일은 제외(별도 토요가산).

    deadline의 공휴일 테이블 재사용 + DB `common.holidays`(date 집합) 주입 지원(계산검토 M-8 —
    코드 하드테이블은 연도별 갱신이라 누락 시 설·추석 정당 가산을 오탐. DB 우선·코드 폴백).
    """
    from erp.claims import deadline
    if d.weekday() == 6:                                  # 일요일
        return True
    if deadline.is_fixed_holiday(d):                      # 고정 양력 공휴일(시행 연도 반영)
        return True
    if extra_holidays and d in extra_holidays:            # DB 캘린더(음력·대체 — 우선)
        return True
    return (d.month, d.day) in deadline.EXTRA_HOLIDAYS.get(d.year, set())  # 코드 폴백


def _surcharge_check(lines, rule, fee):
    """야간·공휴(·토요) 가산 적정성 — 가산코드인데 진료시각이 가산대가 아니면 적발.

    params {surcharge_suffix(코드 접미) | surcharge_codes[], night_start, night_end,
            saturday_surcharge(토요가산 옵트인·기본 OFF)}.
    treated_at(시각) 없는 라인은 판정 보류(미적발).

    **토요가산 옵트인(정책)**: saturday_surcharge=True면 토요일 진료를 정당(전일 가산)으로 보아
    미적발. 기본 OFF=현행(토요일 주간은 적발). 야간전용 코드와 토요가산 코드는 rule을 분리하고
    각 rule의 surcharge_codes/suffix로 대상을 좁혀 구분한다(야간 rule은 OFF, 토요 rule은 ON).
    """
    p = rule.params or {}
    suffix = p.get("surcharge_suffix")
    codes = set(p.get("surcharge_codes") or [])
    ns, ne = p.get("night_start", 20), p.get("night_end", 8)
    sat_opt = bool(p.get("saturday_surcharge", False))   # 토요가산 옵트인(기본 OFF=현행)
    extra_holidays = fee.get("_extra_holidays") if isinstance(fee, dict) else None  # DB 캘린더(M-8)
    out = []
    for ln in lines:
        code, at = ln.get("code") or "", ln.get("treated_at")
        is_target = (code in codes) or (suffix and code.endswith(suffix))
        if not is_target or at is None:
            continue
        at = _to_kst(at)         # tz-aware(UTC 저장·asyncpg) → KST 로컬로 환산 후 가산대 판정
        hour = at.hour
        is_night = hour >= ns or hour < ne
        is_holiday = _is_surcharge_holiday(at.date(), extra_holidays)   # 일요일+법정공휴일(DB 우선)
        is_saturday_ok = sat_opt and at.weekday() == 5       # 토요가산 활성 시 토요일 정당(전일)
        if not (is_night or is_holiday or is_saturday_ok):
            kinds = f"야간({ns}~{ne}시)·공휴" + ("·토요" if sat_opt else "")
            out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                               code, f"가산코드인데 진료시각 {hour:02d}시 — {kinds} 아님",
                               ln.get("treated_on")))
    return out


def _los_ratio(lines, rule, fee):
    """일별 행위량 이상 — 같은 차트·진료일 라인 합계가 per_day_max 초과."""
    mx = (rule.params or {}).get("per_day_max", 30)
    agg: dict = {}
    for ln in lines:
        if ln.get("treated_on") is None:
            continue
        if _is_drug_material(ln):   # 약제·재료 투여량은 행위량 대상 아님 — _count_limit·_period_limit 동형
            continue
        c = _norm_count(ln)      # 취소(0)·조정(음수) 제외 — _count_limit·_period_limit 동형
        if c <= 0:
            continue
        key = (ln.get("chart_no"), ln.get("treated_on"))
        agg[key] = agg.get(key, 0) + c
    return [Finding(rule.code, rule.name, rule.severity, chart or "", "",
                    f"일별 행위량 {cnt}건 (상한 {mx}) — {day}", day)
            for (chart, day), cnt in agg.items() if cnt > mx]


def _percentile(sorted_vals: list, pct: float) -> float:
    """선형보간 백분위 — 최근접 인덱스(round)가 소표본서 p{높음}→최댓값 인덱스로 붕괴해
    임계=최댓값이 되어 룰이 미발화(미탐)하던 문제 해소. numpy 기본(linear)과 동일 방식."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (n - 1) * pct / 100
    lo = int(k)
    hi = min(lo + 1, n - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def _stat_outlier(lines, rule, fee):
    """배치 내 코드별 단가(청구액/횟수) 분포 이상 — Tier2 (PR-B).

    params {percentile, factor, min_samples}: 표본 충분한 코드만, p{percentile}×factor 초과 적발.
    고정 허용치(AMOUNT_OUTLIER)와 보완 관계 — 대량 붙여넣기 평가에서 유효.
    """
    p = rule.params or {}
    pct, factor = p.get("percentile", 95), p.get("factor", 1.5)
    min_n = p.get("min_samples", 10)
    by_code: dict = {}
    for ln in lines:
        code, amt, cnt = ln.get("code"), ln.get("amount"), _norm_count(ln)
        if code and amt is not None and cnt > 0:   # 취소(0)·조정(음수) 라인 제외 — 단가 왜곡 방지
            by_code.setdefault(code, []).append((ln, amt / cnt))
    out = []
    for code, pairs in by_code.items():
        if len(pairs) < min_n:
            continue
        units = sorted(u for _, u in pairs)
        threshold = _percentile(units, pct) * factor
        for ln, unit in pairs:
            if unit > threshold:
                out.append(Finding(rule.code, rule.name, rule.severity,
                                   ln.get("chart_no") or "", code,
                                   f"단가 {unit:,.0f} > 배치 p{pct}×{factor} 기준 "
                                   f"{threshold:,.0f} (표본 {len(units)})", ln.get("treated_on")))
    return out


def _noncovered_claim(lines, rule, fee):
    """비급여 항목의 급여 청구 적발 (고시 전문 비급여 목록 기반) — BLOCK.

    fee[code].type == NONCOVERED 인 코드가 청구 라인에 포함되면 차단.
    (비급여는 환자 전액 부담 — 요양급여 청구 대상이 아님)
    """
    return [Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                    ln.get("code") or "",
                    f"{ln.get('code')}은(는) 행위 비급여 목록 항목 — 급여 청구 불가",
                    ln.get("treated_on"))
            for ln in lines
            if ln.get("code") and _ctype(fee, ln.get("code")) == "NONCOVERED"]


def _eligibility_invalid(lines, rule, fee):
    """자격상실(EXPIRED)·미확인(NOT_FOUND) 환자의 급여 청구 적발 (G11) — 보험 거부 위험.

    비급여(NONCOVERED·전액 본인부담)는 자격 무관이므로 제외. 급여 삭감룰 필터 밖에서 실행
    (_BENEFIT_ONLY_RULES 미포함 — 자격상실 라인은 급여룰에서 제외되므로 별도 점검 필요).
    """
    out, seen = [], set()
    for ln in lines:
        es = (ln.get("eligibility_status") or "").upper()
        if es not in ("EXPIRED", "NOT_FOUND"):
            continue
        if (ln.get("coverage") or "NHIS").upper() == "NONCOVERED":
            continue
        key = (ln.get("chart_no"), ln.get("code"), ln.get("treated_on"))
        if key in seen:
            continue
        seen.add(key)
        label = "자격상실" if es == "EXPIRED" else "자격 미확인"
        out.append(Finding(rule.code, rule.name, rule.severity, ln.get("chart_no") or "",
                           ln.get("code") or "", f"{label} 환자 급여청구 — 보험 거부 위험",
                           ln.get("treated_on")))
    return out


_EVALUATORS = {"DUPLICATE": _duplicate, "COUNT_LIMIT": _count_limit,
               "ELIGIBILITY_INVALID": _eligibility_invalid,
               "UNKNOWN_CODE": _unknown_code, "AMOUNT_OUTLIER": _amount_outlier,
               "AGE_SEX_LIMIT": _age_sex_limit, "EXCLUSIVE_PAIR": _exclusive_pair,
               "PERIOD_LIMIT": _period_limit, "SURCHARGE_CHECK": _surcharge_check,
               "LOS_RATIO": _los_ratio, "STAT_OUTLIER": _stat_outlier,
               "NONCOVERED_CLAIM": _noncovered_claim}

# 급여 삭감 기준 룰 — 비급여(전액 본인부담·기관 자율가) 라인은 평가 대상 아님(false WARN 방지).
# UNKNOWN_CODE 포함: 비급여 코드·가격은 HIS 소관(NonCoveredItem)이라 ERP 마스터 미등재가
# 정상이고 보험 삭감 위험도 없음 → 비급여 라인은 미등재 적발 제외(급여만 적용).
# 단 NONCOVERED_CLAIM(비급여를 급여로 청구하는 오류 적발)은 비급여 포함 전체 유지.
_BENEFIT_ONLY_RULES = frozenset({
    "DUPLICATE", "COUNT_LIMIT", "AMOUNT_OUTLIER", "AGE_SEX_LIMIT", "EXCLUSIVE_PAIR",
    "PERIOD_LIMIT", "SURCHARGE_CHECK", "LOS_RATIO", "STAT_OUTLIER", "UNKNOWN_CODE"})


def _is_noncovered(ln: dict, fee: dict) -> bool:
    if (ln.get("coverage") or "").upper() == "NONCOVERED":
        return True
    fm = fee.get(ln.get("code") or "")
    return isinstance(fm, dict) and fm.get("type") == "NONCOVERED"


def _no_benefit_claim(ln: dict, fee: dict) -> bool:
    """급여 삭감 룰 평가 대상 아님 — 비급여 또는 자격상실/미확인.

    자격상실(EXPIRED)·자격없음(NOT_FOUND)은 보험 청구 대상이 아님(보험 0원·전액 본인부담)
    → 심평원 삭감 위험이 없으므로 급여 삭감 룰 제외(비급여와 동일). 소급 자격복원 시 재청구는
    재-sync로 재평가됨.
    """
    if _is_noncovered(ln, fee):
        return True
    return (ln.get("eligibility_status") or "").upper() in ("EXPIRED", "NOT_FOUND")


class PreReviewEngine:
    def evaluate(self, *, lines: list[dict], rules, fee: dict) -> list[Finding]:
        findings: list[Finding] = []
        covered = [ln for ln in lines if not _no_benefit_claim(ln, fee)]
        for rule in rules:
            if not getattr(rule, "active", True):
                continue
            fn = _EVALUATORS.get(rule.rule_type)
            if not fn:
                continue
            # 급여 기준 룰은 비급여 라인 제외(보험 청구 대상 아님)
            target = covered if rule.rule_type in _BENEFIT_ONLY_RULES else lines
            try:
                rfindings = fn(target, rule, fee)
            except Exception as exc:  # noqa: BLE001 — 한 룰(라인 타입 이상 등) 실패가 나머지 룰
                # findings를 통째로 죽이지 않게 격리(전 배치 삭감 미탐=조용한 실패 방지)·로깅 가시화.
                logger.warning("사전심사 룰 평가 실패 — 격리·계속", extra={"ctx": {
                    "rule": getattr(rule, "code", None), "type": rule.rule_type, "err": str(exc)[:120]}})
                continue
            grp, rcode = REASON_BY_TYPE.get(rule.rule_type, ("", ""))
            for f in rfindings:          # 거부사유 코드 스탬프(룰타입 기준·평가함수 비수정)
                f.reason_group, f.reason_code = grp, rcode
            findings.extend(rfindings)
        return findings

    def evaluate_grade(self, *, lines: list[dict], grade) -> list[Finding]:
        """종별 정합(P1 G6) — HIS 동봉 종별(institution_grade·#75) vs ERP 종별마스터 불일치 적발.

        불일치 = ERP 종별가산·본인부담률 오적용 위험(차트당 1건). ERP 미설정인데 HIS 종별 있으면
        마스터 설정 필요 경고. HIS 종별 미동봉 시 점검 불가(skip). 실제 불일치 시에만 발화(비스팸).
        """
        g = (grade or "").upper()
        out, seen = [], set()
        for ln in lines:
            if (ln.get("coverage") or "NHIS").upper() == "NONCOVERED":
                continue
            his_g = (ln.get("institution_grade") or "").upper()
            ch = ln.get("chart_no")
            if not his_g or not ch or ch in seen:
                continue
            if g and his_g != g:
                seen.add(ch)
                out.append(Finding("GRADE-MISMATCH", "종별 불일치", "WARN", ch, "",
                                   f"HIS 종별({his_g}) ≠ ERP 종별({g}) — 종별가산·본인부담률 정합 확인",
                                   None, "B", "B-GRADE"))
            elif not g:
                seen.add(ch)
                out.append(Finding("GRADE-UNSET", "ERP 종별 미설정", "WARN", ch, "",
                                   f"ERP 종별 미설정(HIS={his_g}) — 종별마스터 설정 필요", None, "B", "B-GRADE"))
        return out

    def evaluate_bundle(self, *, lines: list[dict], bundle_rules) -> list[Finding]:
        """묶음청구(P1 G1/G4) — 패키지 수가(parent)와 포함 세부/재료(child) 동시청구 시 적발.

        동일 진료일·차트에 parent + child가 함께 청구되면 child는 별도산정 불가(언번들링).
        rule_type=BUNDLE→BUNDLE_VIOLATION·MATERIAL→MATERIAL_UNBUNDLING(거부사유 매핑).
        """
        rules = [r for r in bundle_rules if getattr(r, "active", True) and r.parent_code]
        if not rules:
            return []
        grp: dict = {}
        for ln in lines:
            grp.setdefault((ln.get("chart_no"), ln.get("treated_on")), set()).add(ln.get("code"))
        out, seen = [], set()
        for (chart, day), codes in grp.items():
            for r in rules:
                if r.parent_code not in codes:
                    continue
                children = [c.strip() for c in (r.child_codes or "").split(",") if c.strip()]
                rtype = "MATERIAL_UNBUNDLING" if r.rule_type == "MATERIAL" else "BUNDLE_VIOLATION"
                rg, rc = REASON_BY_TYPE[rtype]
                for ch in children:
                    if ch in codes and (chart, ch, day) not in seen:
                        seen.add((chart, ch, day))
                        out.append(Finding(f"BUNDLE-{r.rule_type}", r.name or "묶음청구 위반",
                                           r.severity, chart or "", ch,
                                           f"{r.parent_code} 포함 {ch} 별도청구"
                                           f"{': ' + r.reason if r.reason else ''}", day, rg, rc))
        return out

    def evaluate_dx(self, *, lines: list[dict], dx_rules) -> list[Finding]:
        """진단(KCD)-수가 연계(P1 G2) — 수가에 필수 상병 prefix 미기재 시 형식 적발.

        라인 `diagnosis_codes`(쉼표 KCD)를 차트 단위로 합집합 → 수가의 필수 prefix 중 하나도
        없으면 DIAGNOSIS_MISMATCH. **형식 점검만**(임상 타당성은 의료진 게이트). 차트·코드·일자 1건.
        """
        by_code = {r.code: r for r in dx_rules if getattr(r, "active", True) and r.code}
        if not by_code:
            return []
        chart_dx: dict = {}
        for ln in lines:
            dxs = ln.get("diagnosis_codes")
            if dxs:
                s = chart_dx.setdefault(ln.get("chart_no"), set())
                s.update(d.upper() for d in str(dxs).replace(" ", "").split(",") if d)
        cg, cc = REASON_BY_TYPE["DIAGNOSIS_MISMATCH"]
        out, seen = [], set()
        for ln in lines:
            r = by_code.get(ln.get("code"))
            if not r:
                continue
            prefixes = [p.strip().upper() for p in (r.require_dx_prefixes or "").split(",") if p.strip()]
            if not prefixes:
                continue
            if ln.get("chart_no") not in chart_dx:   # 상병 전무(HIS 미동봉) — 데이터갭 vs 실제누락
                continue                              # 구분 불가라 판정 보류(형식점검 오탐 억제). 상병 있으면 부합 검증
            dxset = chart_dx.get(ln.get("chart_no"), set())
            if any(d.startswith(pfx) for d in dxset for pfx in prefixes):
                continue
            key = (ln.get("chart_no"), ln.get("code"), ln.get("treated_on"))
            if key in seen:
                continue
            seen.add(key)
            out.append(Finding("DX-MISMATCH", r.name or "상병-수가 미부합", r.severity,
                               ln.get("chart_no") or "", ln.get("code") or "",
                               f"수가 {ln.get('code')} 필수상병({'/'.join(prefixes)}) 미기재"
                               f"{': ' + r.reason if r.reason else ''}", ln.get("treated_on"), cg, cc))
        return out

    def evaluate_dur(self, *, lines: list[dict], dur_rules) -> list[Finding]:
        """약제 DUR(P0-1) — 병용금기(CONTRA 성분쌍)·동일성분 중복(DUP)·연령금기(AGE).

        라인에 `ingredient`(주성분코드) 부여 전제(worklist_service가 drug_ingredient_map으로 enrich).
        동일 진료일·동일 차트의 DRUG 라인을 성분 단위로 묶어 점검. severity는 dur_rules.severity.
        임부금기(PREGNANCY)는 HIS pregnancy_status(#72) 수신분으로 점검.
        """
        contra, age, preg, dup_on = {}, {}, {}, False
        for r in dur_rules:
            if not getattr(r, "active", True):
                continue
            if r.dur_type == "CONTRA" and r.ingredient_code and r.ingredient_code_b:
                contra[frozenset({r.ingredient_code, r.ingredient_code_b})] = r
            elif r.dur_type == "AGE" and r.ingredient_code:
                age.setdefault(r.ingredient_code, []).append(r)
            elif r.dur_type == "PREGNANCY" and r.ingredient_code:
                preg.setdefault(r.ingredient_code, []).append(r)
            elif r.dur_type == "DUP":
                dup_on = True
        grp: dict = {}
        for ln in lines:
            if (ln.get("code_type") or "").upper() != "DRUG" or not ln.get("ingredient"):
                continue
            grp.setdefault((ln.get("chart_no"), ln.get("treated_on")), []).append(ln)
        cg, cc = REASON_BY_TYPE["DRUG_CONTRA"]
        dg, dc = REASON_BY_TYPE["DRUG_DUP_THERAPY"]
        out: list[Finding] = []
        for (chart, day), gln in grp.items():
            by_ing: dict = {}
            for ln in gln:
                by_ing.setdefault(ln["ingredient"], []).append(ln)
            ings = list(by_ing)
            for a_i in range(len(ings)):           # 병용금기 성분쌍
                for b_i in range(a_i + 1, len(ings)):
                    r = contra.get(frozenset({ings[a_i], ings[b_i]}))
                    if r:
                        ln = by_ing[ings[a_i]][0]
                        out.append(Finding("DUR-CONTRA", "병용금기", r.severity, chart or "",
                                           ln.get("code") or "",
                                           f"병용금기 {ings[a_i]}+{ings[b_i]}: {r.reason or ''}"
                                           f"{' ('+r.notice_no+')' if r.notice_no else ''}",
                                           day, cg, cc))
            for ing, lns in by_ing.items():        # 동일성분 중복(서로 다른 코드 2+)
                codes = {x.get("code") for x in lns}
                if dup_on and len(codes) > 1:
                    out.append(Finding("DUR-DUP", "동일성분 중복", "WARN", chart or "",
                                       sorted(c for c in codes if c)[0],
                                       f"동일성분({ing}) 중복: {sorted(codes)}",
                                       day, dg, dc))
                for r in age.get(ing, []):         # 연령금기
                    pa = lns[0].get("age")          # 라인 dict 키 'age'(patient_age 폴백)
                    if pa is None:
                        pa = lns[0].get("patient_age")
                    if pa is None:
                        continue
                    if (r.min_age is not None and pa < r.min_age) or \
                       (r.max_age is not None and pa > r.max_age):
                        out.append(Finding("DUR-AGE", "연령금기", r.severity, chart or "",
                                           lns[0].get("code") or "",
                                           f"연령금기 {ing}(연령 {pa}): {r.reason or ''}", day, cg, cc))
                if preg.get(ing):                  # 임부금기(임신상태 동봉 시)
                    ps = (lns[0].get("pregnancy_status") or "").upper()
                    if ps in ("PREGNANT", "Y", "TRUE", "PREGNANCY"):
                        r = preg[ing][0]
                        out.append(Finding("DUR-PREG", "임부금기", r.severity, chart or "",
                                           lns[0].get("code") or "",
                                           f"임부금기 {ing}: {r.reason or ''}", day, cg, cc))
        return out
