"""약품 코드 매칭 엔진 (D-6) — HIS 약품 명칭/성분 → 보험 제품코드 후보.

브랜드명+용량으로 fee_master(DRUG) ILIKE 검색 + 신뢰도. 일회성 스크립트가 아닌
상시 서비스 — billing 수신/관리 화면에서 미등재 약품 자동 후보 매칭.
"""
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.regulatory.models import FeeMaster

_DOSE = re.compile(r"(\d[\d.]*)\s*(mg|mL|g|IU|mcg|단위|U)", re.IGNORECASE)
_FORM = re.compile(r"(정|캡슐|주사액|주사|용액|시럽|연고|크림)")
# 제형군 — 오매칭 방지(정↔주사·현탁액 구분). 키워드→군.
_FORM_GROUP = {
    "정": "PO_SOLID", "캡슐": "PO_SOLID",
    "시럽": "PO_LIQ", "현탁액": "PO_LIQ", "건조시럽": "PO_LIQ",
    "주사": "INJ", "주": "INJ", "주사액": "INJ", "주사제": "INJ",
    "점안": "EYE", "안연고": "EYE", "연고": "TOP", "크림": "TOP", "겔": "TOP",
}
# 후보가 HIS와 다른 제형군이면 제외할 키워드(군별 대표어)
_FORM_EXCLUDE = {
    "PO_SOLID": ["시럽", "현탁액", "주사", "점안", "안연고", "연고", "크림", "외용", "패취"],
    "PO_LIQ": ["주사", "점안", "안연고", "연고", "패취"],
    "INJ": ["정", "캡슐", "시럽", "점안", "안연고", "연고", "시럽"],
    "EYE": ["정", "캡슐", "주사"],
    "TOP": ["정", "캡슐", "주사", "점안"],
}


def _form_group(name: str) -> str | None:
    for kw, grp in _FORM_GROUP.items():
        if kw in (name or ""):
            return grp
    return None


def _brand_dose(name: str) -> tuple[str, str | None]:
    """제품명 → (브랜드, 용량숫자). '셀레브렉스캡슐 200mg' → ('셀레브렉스','200')."""
    brand = _FORM.split(name or "")[0].strip()
    brand = re.sub(r"\(.*?\)", "", brand).strip()
    d = _DOSE.search(name or "")
    return brand, (d.group(1) if d else None)


async def suggest(session: AsyncSession, *, drug_name: str | None,
                  ingredient: str | None = None, limit: int = 5) -> dict:
    """약품 → 보험코드 후보 + 신뢰도.

    반환: {confidence, candidates:[{code,name,unit_price}], matched_name?, insurance_code?}
    HIGH=브랜드+용량 단일 후보(용량 미기재는 MEDIUM), MEDIUM=다중 후보·표준코드 브리지·용량없음
    단일, LOW=성분 폴백, None=미매칭.
    """
    # 혈액제제는 혈액수가 체계 — 약가 마스터 비대상(미매칭이 정상, HIS 회신 반영)
    blood = ("적혈구", "혈장", "혈소판", "동결침전", "전혈", "농축적혈구", "FFP")
    if any(k in (drug_name or "") or k in (ingredient or "") for k in blood):
        return {"confidence": None, "candidates": [], "insurance_code": None,
                "matched_name": "혈액수가 체계(약가 비대상)"}

    brand, dose = _brand_dose(drug_name or "")
    form_grp = _form_group(drug_name or "")
    cands: list[dict] = []

    def _form_ok(cand_name: str) -> bool:
        """후보 제형군이 HIS와 충돌하지 않아야(오매칭 방지: 정↔주사·현탁액·소아)."""
        if form_grp is None:
            return True
        for ex in _FORM_EXCLUDE.get(form_grp, []):
            if ex in cand_name:
                return False
        return True

    async def _q(*likes: str) -> list[dict]:
        stmt = select(FeeMaster.code, FeeMaster.name, FeeMaster.unit_price).where(
            FeeMaster.code_type == "DRUG", FeeMaster.valid_to.is_(None))
        for lk in likes:
            stmt = stmt.where(FeeMaster.name.ilike(lk))
        # 제형 불일치 후보 제외 후 limit (넉넉히 조회 후 필터)
        rows = (await session.execute(stmt.order_by(FeeMaster.name).limit(limit * 4))).all()
        out = [{"code": c, "name": n, "unit_price": float(p)}
               for c, n, p in rows if _form_ok(n)]
        return out[:limit]

    if brand and len(brand) >= 2:
        likes = [f"%{brand}%"] + ([f"%{dose}%"] if dose else [])
        cands = await _q(*likes)
    # 성분 폴백 (브랜드 미매칭)
    if not cands and ingredient:
        ing = re.sub(r"[/·,].*$", "", ingredient).strip()
        if len(ing) >= 2:
            likes = [f"%{ing}%"] + ([f"%{dose}%"] if dose else [])
            cands = await _q(*likes)
            for c in cands:
                c["by_ingredient"] = True

    # 표준코드 마스터 폴백 — fee_master 표기와 다른 상품명(포장·업체 병기 등)을 표준코드
    # 마스터(상품명)로 브리지: 마스터 히트 → 제품코드 → fee_master(현행 DRUG) 검증 후 후보.
    # 마스터에만 있고 약가 미등재면 후보 아님(청구 불가 코드 방지). 취소 품목 제외.
    if not cands and brand and len(brand) >= 2:
        from erp.regulatory.models import DrugStdCode
        stmt = (select(DrugStdCode.product_code, DrugStdCode.name)
                .where(DrugStdCode.product_code.isnot(None),
                       DrugStdCode.canceled_on.is_(None),
                       DrugStdCode.name.ilike(f"%{brand}%")))
        if dose:
            stmt = stmt.where(DrugStdCode.name.ilike(f"%{dose}%"))
        std_hits = (await session.execute(stmt.limit(limit * 4))).all()
        codes = list({pc for pc, nm in std_hits if _form_ok(nm)})
        if codes:
            rows = (await session.execute(
                select(FeeMaster.code, FeeMaster.name, FeeMaster.unit_price)
                .where(FeeMaster.code_type == "DRUG", FeeMaster.valid_to.is_(None),
                       FeeMaster.code.in_(codes)).order_by(FeeMaster.name))).all()
            cands = [{"code": c, "name": n, "unit_price": float(p), "by_std_code": True}
                     for c, n, p in rows][:limit]

    if not cands:
        return {"confidence": None, "candidates": [], "insurance_code": None}
    by_ing = cands[0].get("by_ingredient")
    by_std = cands[0].get("by_std_code")
    # HIGH(자동확정)는 브랜드+용량 단일 후보만 — 용량 미기재면 교차검증 불가라 MEDIUM(약제팀 확인).
    # by_ing(성분 폴백)=LOW·by_std(표준코드 브리지)=MEDIUM 상한은 이름 매칭 경유라 자동확정 방지.
    confidence = ("LOW" if by_ing
                  else "MEDIUM" if by_std
                  else ("HIGH" if len(cands) == 1 and dose else "MEDIUM"))
    return {"confidence": confidence, "candidates": cands,
            "matched_name": cands[0]["name"],
            "insurance_code": cands[0]["code"] if confidence == "HIGH" else None}
