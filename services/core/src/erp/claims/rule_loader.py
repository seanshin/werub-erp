"""사전심사 룰 적재 로더 — 멱등 upsert (순수 함수, commit/audit는 호출부 제어).

라우터 적재 EP(claims/router.py)와 시더 스크립트(scripts/seed_prereview_rules.py)가
동일 로직을 공유(SoT 단일화 — 중복·드리프트 방지). Row 검증은 pydantic으로 양쪽 공통.

DUR 등 룰 데이터는 식약처/HIRA 공개데이터(라이선스 유형2 출처표시·비상업) 기반이며,
임상 타당성은 의료진 게이트(의료법)·약제팀 확인 후 운영 반영. 빈 상태면 적발 0(무영향).
"""
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.models import BundlingRule, DrugIngredientMap, DurRule, DxProcedureRule
from erp.regulatory.models import InstitutionSurchargeRate


# ── Row 스키마 (라우터·시더 공용) ─────────────────────────────
class DurIngredientRow(BaseModel):
    insurance_code: str
    ingredient_code: str
    ingredient_name: str | None = None


class DurRuleRow(BaseModel):
    dur_type: Literal["CONTRA", "DUP", "AGE", "PREGNANCY"]
    ingredient_code: str | None = None
    ingredient_code_b: str | None = None
    min_age: int | None = None
    max_age: int | None = None
    reason: str | None = None
    notice_no: str | None = None
    severity: Literal["WARN", "BLOCK"] = "BLOCK"
    source: str = "MFDS"


class DxRuleRow(BaseModel):
    code: str
    require_dx_prefixes: str            # KCD prefix 쉼표구분(예: "C,D0")
    name: str | None = None
    reason: str | None = None
    severity: Literal["WARN", "BLOCK"] = "WARN"


class BundleRuleRow(BaseModel):
    parent_code: str
    child_codes: str                    # 쉼표구분
    rule_type: Literal["BUNDLE", "MATERIAL"] = "BUNDLE"
    name: str | None = None
    reason: str | None = None
    severity: Literal["WARN", "BLOCK"] = "WARN"


class SurchargeRateRow(BaseModel):
    grade: str                          # CLINIC|HOSPITAL|GENERAL|SUPERIOR
    # 종별가산율 — 분수(0.15=15%). ge=0(가산 정의상 양수)·le=1.0(≤100% 상한: 0.15→1.5, 15 오입력
    # 차단). 검증 없으면 fee×(1+rate) 기대단가 폭증→사전심사 AMOUNT_OUTLIER 오탐/누락(revenue 정합).
    rate: float = Field(ge=0, le=1.0)   # 가산율(예: 0.15)
    applies_to: str = "TARIFF"          # 적용 code_type(쉼표)


# ── 로더 (멱등 upsert — commit/audit 미수행) ──────────────────
async def load_ingredient_map(session: AsyncSession, rows: list[DurIngredientRow]) -> int:
    """보험코드→주성분 매핑 — insurance_code 자연키 upsert."""
    for r in rows:
        ex = await session.scalar(select(DrugIngredientMap).where(
            DrugIngredientMap.insurance_code == r.insurance_code))
        if ex:
            ex.ingredient_code, ex.ingredient_name = r.ingredient_code, r.ingredient_name
        else:
            session.add(DrugIngredientMap(**r.model_dump()))
    return len(rows)


async def load_dur_rules(session: AsyncSession, rows: list[DurRuleRow], *,
                         replace_source: str | None = None) -> int:
    """DUR 룰 — replace_source 지정 시 해당 출처 전체 교체(고시 갱신), 아니면 자연키 멱등."""
    if replace_source:
        await session.execute(delete(DurRule).where(DurRule.source == replace_source))
    for r in rows:
        # 자연키(타입·성분쌍·연령) 일치 시 갱신, 없으면 추가 — 재적재 중복 누적 방지
        ex = None if replace_source else await session.scalar(select(DurRule).where(
            DurRule.dur_type == r.dur_type, DurRule.ingredient_code == r.ingredient_code,
            DurRule.ingredient_code_b == r.ingredient_code_b,
            DurRule.min_age == r.min_age, DurRule.max_age == r.max_age))
        if ex:
            ex.reason, ex.notice_no, ex.severity, ex.source = (
                r.reason, r.notice_no, r.severity, r.source)
        else:
            session.add(DurRule(**r.model_dump()))
    return len(rows)


async def load_dx_rules(session: AsyncSession, rows: list[DxRuleRow]) -> int:
    """진단(KCD)-수가 연계 룰 — code 자연키 upsert."""
    for r in rows:
        ex = await session.scalar(select(DxProcedureRule).where(DxProcedureRule.code == r.code))
        if ex:
            ex.require_dx_prefixes, ex.name, ex.reason, ex.severity = (
                r.require_dx_prefixes, r.name, r.reason, r.severity)
        else:
            session.add(DxProcedureRule(**r.model_dump()))
    return len(rows)


async def load_bundling_rules(session: AsyncSession, rows: list[BundleRuleRow]) -> int:
    """묶음청구 룰 — (parent_code, rule_type, child_codes) 자연키 upsert."""
    for r in rows:
        ex = await session.scalar(select(BundlingRule).where(
            BundlingRule.parent_code == r.parent_code, BundlingRule.rule_type == r.rule_type,
            BundlingRule.child_codes == r.child_codes))
        if ex:
            ex.name, ex.reason, ex.severity = r.name, r.reason, r.severity
        else:
            session.add(BundlingRule(**r.model_dump()))
    return len(rows)


async def load_surcharge_rates(session: AsyncSession, rows: list[SurchargeRateRow]) -> int:
    """종별가산율 — grade 자연키 upsert."""
    for r in rows:
        ex = await session.scalar(select(InstitutionSurchargeRate).where(
            InstitutionSurchargeRate.grade == r.grade))
        if ex:
            ex.rate, ex.applies_to = r.rate, r.applies_to
        else:
            session.add(InstitutionSurchargeRate(**r.model_dump()))
    return len(rows)


# 시더·라우터 공용 — 룰 종류 → (Row 모델, 로더). dur_rules는 replace_source 옵션 별도.
LOADERS = {
    "ingredient_map": (DurIngredientRow, load_ingredient_map),
    "dur_rules": (DurRuleRow, load_dur_rules),
    "dx_rules": (DxRuleRow, load_dx_rules),
    "bundling_rules": (BundleRuleRow, load_bundling_rules),
    "surcharge_rates": (SurchargeRateRow, load_surcharge_rates),
}
