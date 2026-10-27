"""사전심사 룰 로더 — 멱등 upsert·replace_source (라우터 EP·시더 공유 SoT)."""
from sqlalchemy import func, select

from erp.claims.models import BundlingRule, DrugIngredientMap, DurRule, DxProcedureRule
from erp.claims.rule_loader import (
    BundleRuleRow,
    DurIngredientRow,
    DurRuleRow,
    DxRuleRow,
    SurchargeRateRow,
    load_bundling_rules,
    load_dur_rules,
    load_dx_rules,
    load_ingredient_map,
    load_surcharge_rates,
)
from erp.regulatory.models import InstitutionSurchargeRate


async def _count(session, model) -> int:
    return int(await session.scalar(select(func.count()).select_from(model)))


async def test_dur_rules_idempotent_natural_key(session):
    rows = [DurRuleRow(dur_type="CONTRA", ingredient_code="A", ingredient_code_b="B",
                       reason="병용금기", severity="BLOCK", source="MFDS"),
            DurRuleRow(dur_type="AGE", ingredient_code="C", max_age=12, reason="소아", source="MFDS")]
    assert await load_dur_rules(session, rows) == 2
    await session.flush()
    # 재적재 — 자연키 일치 → 갱신만(중복 누적 없음)
    rows[0] = DurRuleRow(dur_type="CONTRA", ingredient_code="A", ingredient_code_b="B",
                         reason="병용금기(개정)", severity="WARN", source="MFDS")
    await load_dur_rules(session, rows)
    await session.flush()
    assert await _count(session, DurRule) == 2
    updated = await session.scalar(select(DurRule).where(DurRule.dur_type == "CONTRA"))
    assert updated.reason == "병용금기(개정)" and updated.severity == "WARN"


async def test_dur_replace_source_swaps_whole_source(session):
    await load_dur_rules(session, [DurRuleRow(dur_type="DUP", reason="구", source="MFDS")])
    await session.flush()
    # replace_source — 해당 출처 전체 교체(고시 갱신)
    await load_dur_rules(session, [DurRuleRow(dur_type="CONTRA", ingredient_code="X",
                                              reason="신", source="MFDS")],
                         replace_source="MFDS")
    await session.flush()
    rows = (await session.scalars(select(DurRule).where(DurRule.source == "MFDS"))).all()
    assert len(rows) == 1 and rows[0].dur_type == "CONTRA"


async def test_ingredient_map_upsert(session):
    await load_ingredient_map(session, [DurIngredientRow(insurance_code="D1", ingredient_code="A")])
    await session.flush()
    await load_ingredient_map(session, [DurIngredientRow(insurance_code="D1", ingredient_code="A2",
                                                         ingredient_name="갱신")])
    await session.flush()
    assert await _count(session, DrugIngredientMap) == 1
    row = await session.scalar(select(DrugIngredientMap).where(DrugIngredientMap.insurance_code == "D1"))
    assert row.ingredient_code == "A2" and row.ingredient_name == "갱신"


async def test_dx_bundling_surcharge_upsert(session):
    await load_dx_rules(session, [DxRuleRow(code="P1", require_dx_prefixes="C,D0")])
    await load_bundling_rules(session, [BundleRuleRow(parent_code="PKG", child_codes="S1,S2")])
    await load_surcharge_rates(session, [SurchargeRateRow(grade="HOSPITAL", rate=0.20)])
    await session.flush()
    assert await _count(session, DxProcedureRule) == 1
    assert await _count(session, BundlingRule) == 1
    # 종별가산 재적재 — grade 자연키 갱신(중복 없음)
    await load_surcharge_rates(session, [SurchargeRateRow(grade="HOSPITAL", rate=0.30)])
    await session.flush()
    assert await _count(session, InstitutionSurchargeRate) == 1
    sr = await session.scalar(select(InstitutionSurchargeRate).where(
        InstitutionSurchargeRate.grade == "HOSPITAL"))
    assert float(sr.rate) == 0.30


def test_surcharge_rate_range_validated():
    """심층 재검토 — 종별가산율 부호/범위 검증(0.15를 1.5로·15로·음수 오입력 차단)."""
    import pytest
    from pydantic import ValidationError
    SurchargeRateRow(grade="SUPERIOR", rate=0.30)      # 정상(30%)
    for bad in (1.5, 15, -0.10):                       # >100%·백분율 오입력·음수(가산은 양수)
        with pytest.raises(ValidationError):
            SurchargeRateRow(grade="X", rate=bad)


def test_sample_seed_files_parse():
    """동봉 SAMPLE 시드가 Row 스키마로 파싱되는지(형식 검증)."""
    import json
    import os
    from erp.claims.rule_loader import LOADERS
    base = os.path.join(os.path.dirname(__file__), "..", "seeds", "prereview")
    for kind, (RowModel, _loader) in LOADERS.items():
        path = os.path.join(base, f"{kind}.json")
        with open(path, encoding="utf-8") as fh:
            rows = json.load(fh)
        assert isinstance(rows, list) and rows
        for d in rows:
            RowModel(**d)        # 검증 실패 시 예외
