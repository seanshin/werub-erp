"""EDI 매핑 엔진 (D3-B2 격리 시드) — 산정 결과 → 심평원 청구 구조 **매핑 전담**.

⚠ **격리·시드 단계**: 여기의 세그먼트 구조는 **실 심평원 EDI 포맷이 아니다**(대표 필드만). 실 스펙(B0)
확보 전까지 각 버전 `production_ready=False`·**프로덕션 청구 제출에 연결하지 않는다**(claim_submission 경로
미연결). 미리보기·설계 검증용.

설계 원칙 (사용자 지침 "EDI는 교체도 심하고, 시간도 역행해서 재계산도 빈번"):
  1. **산정과 완전 분리** — 산정(claim_calc)은 회계 계산식, 여기는 순수 포맷 매핑. 포맷이 바뀌어도
     산정 로직 무변경.
  2. **발효일 기반 temporal 버전 레지스트리** — 포맷 교체 = 새 버전 **append**(effective_from 지정).
     과거 버전 엔트리는 **불변**(과거 청구 재현). FeeMaster의 valid_from/valid_to와 동일한 시점 해소 개념.
  3. **시간 역행 재계산** — `resolve_version(as_of)`가 청구/진료 시점에 유효했던 포맷 버전을 선택.
     2025년 청구를 오늘 재생성해도 **2025년 당시 포맷**으로 매핑(오늘 최신 포맷 아님).
  4. **입력 = 산정 결과 dict**(claim_calc.calc_statement) — as-of 단가 이미 반영. 매퍼는 표현만.

실 스펙 확보 시: 실 세그먼트(요양기관·수진자·명세서·진료내역·상병) 매퍼를 새 버전으로 등록(effective_from=
고시 시행일·production_ready=True)하고 `edifact.serialize`(아웃바운드 직렬화 신설)에 연결. 그 전까지 무관.
"""
from dataclasses import dataclass
from datetime import date
from typing import Callable


@dataclass(frozen=True)
class EdiMappingVersion:
    """EDI 매핑 포맷 버전 — 발효일·검증여부·매퍼. 심평원 고시 개정 1건 = 새 엔트리."""
    version: str
    effective_from: date          # 이 포맷 발효일(심평원 고시 시행일에 대응)
    production_ready: bool         # 실 심평원 포맷 검증 완료 여부(실 스펙 확보 시 True)
    description: str
    mapper: Callable[..., dict]


def _map_seed_v0(calc: dict, *, provider_code: str | None) -> dict:
    """시드 매핑 — 산정 결과를 EDI 유사 세그먼트 구조로. **실 심평원 포맷 아님(대표 필드).**"""
    detail = [
        {
            "SEG": "LINE",                       # 진료내역(시드 필드명)
            "code": ln.get("code"),
            "code_type": ln.get("code_type"),
            "unit_price": ln.get("unit_price"),
            "count": ln.get("count"),
            "amount": ln.get("total"),
            "patient": ln.get("patient"),
            "insurer": ln.get("insurer"),
            "benefit": "N" if ln.get("coverage") == "NONCOVERED" else "Y",
        }
        for ln in calc.get("lines", []) if ln.get("priced", True)
    ]
    return {
        "HEAD": {                                 # 요양기관·명세 헤더(시드)
            "provider_code": provider_code,       # 요양기호(미확보 시 None)
            "treated_on": calc.get("treated_on"),
            "grade": calc.get("grade"),
            "formula_version": calc.get("formula_version"),
        },
        "AMT": {                                  # 금액 집계(시드)
            "total": calc.get("total"),
            "claim_amount": calc.get("claim_amount"),     # 공단부담(청구액)
            "patient_share": calc.get("patient_share"),
            "non_covered": calc.get("non_covered"),
        },
        "DETAIL": detail,
        "NOTE": {
            "bundled_excluded": calc.get("bundled_excluded", []),
            "unresolved_codes": calc.get("unresolved_codes", []),
            "warnings": calc.get("warnings", []),
        },
    }


# 발효일 오름차순 레지스트리 — 포맷 교체 = 새 버전 append(과거 버전 불변=소급 재현).
# 실 스펙 확보 시 실 매퍼를 (effective_from=고시일·production_ready=True)로 추가.
_REGISTRY: list[EdiMappingVersion] = [
    EdiMappingVersion("SEED_v0", date(2000, 1, 1), False, "격리 시드(실 심평원 포맷 아님)", _map_seed_v0),
]

# 현행 최신 버전 라벨·검증여부(하위호환 노출) — 상세는 레지스트리가 SoT.
EDI_MAPPING_VERSION = _REGISTRY[-1].version
EDI_PRODUCTION_READY = any(v.production_ready for v in _REGISTRY)


def _pick(registry: list[EdiMappingVersion], as_of: date) -> EdiMappingVersion:
    """발효일 기준 시점 해소(순수 함수·테스트 용이) — as_of 이하 최신 발효 버전.

    as_of가 최초 발효일 이전이면 최초 버전 폴백(음수 방지). 빈 레지스트리는 호출 전 차단.
    """
    eligible = [v for v in registry if v.effective_from <= as_of]
    if not eligible:
        return min(registry, key=lambda v: v.effective_from)
    return max(eligible, key=lambda v: v.effective_from)


def resolve_version(as_of: date) -> EdiMappingVersion:
    """청구/진료 시점(as_of)에 유효한 매핑 버전 — 소급 재계산 시 당시 포맷 사용."""
    return _pick(_REGISTRY, as_of)


def _parse_iso(s) -> date | None:
    if isinstance(s, date):
        return s
    try:
        return date.fromisoformat(str(s)[:10])
    except (ValueError, TypeError):
        return None


def map_to_edi(calc: dict, *, as_of: date | None = None, version: str | None = None,
               provider_code: str | None = None) -> dict:
    """산정 결과 → EDI 매핑(격리 시드).

    버전 선택: `version` 명시 시 그 버전(고정). 아니면 `as_of`(소급 정합) → 없으면 산정 결과의
    `treated_on` → 그것도 없으면 현행 최신. ⚠ 반환은 **시드 구조**(실 심평원 포맷 아님)·해당 버전
    `_production_ready`. 제출 경로 연결 금지.
    """
    if version is not None:
        entry = next((v for v in _REGISTRY if v.version == version), None)
        if entry is None:
            raise ValueError(f"미등록 EDI 매핑 버전: {version} "
                             f"(등록: {sorted(v.version for v in _REGISTRY)})")
    else:
        d = as_of or _parse_iso(calc.get("treated_on"))
        entry = resolve_version(d) if d else _REGISTRY[-1]
    out = entry.mapper(calc, provider_code=provider_code)
    # 버전 메타는 레지스트리가 SoT(매퍼 출력 위에 권위 부여)
    out["_seed"] = True                          # 시드 마커(실 제출 금지 식별)
    out["mapping_version"] = entry.version
    out["mapping_effective_from"] = entry.effective_from.isoformat()
    out["_production_ready"] = entry.production_ready
    return out
