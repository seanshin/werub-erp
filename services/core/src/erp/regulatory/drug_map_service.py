"""약품 코드 매핑 관리 (D-6) — 자동 후보 빌드·확정·조회·해소 코드 제공.

자동 빌드: 미등재 약품(billing_lines DRUG, drug_name 동봉) → 매칭 엔진 후보 → SUGGESTED 적재.
확정: 약제팀이 보험코드 확정(CONFIRMED) → 이후 라인 자동 변환.
"""
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.regulatory.drug_matcher import suggest
from erp.regulatory.models import DrugCodeMap

_audit = AuditService()


class DrugMapService:
    async def build_suggestions(self, session: AsyncSession, *, actor: int) -> dict:
        """미등재 약품 스캔 → 매칭 후보 SUGGESTED 적재(멱등 — 기존 his_code 스킵)."""
        rows = (await session.execute(text("""
            SELECT DISTINCT bl.code, bl.drug_name, bl.drug_ingredient
            FROM patient.billing_lines bl
            LEFT JOIN regulatory.fee_master fm ON fm.code = bl.code AND fm.valid_to IS NULL
            WHERE bl.cancelled = false AND fm.code IS NULL AND bl.code_type = 'DRUG'
        """))).all()
        existing = {m.his_code for m in (await session.scalars(select(DrugCodeMap))).all()}
        created = 0
        for code, name, ingr in rows:
            if code in existing:
                continue
            s = await suggest(session, drug_name=name, ingredient=ingr)
            session.add(DrugCodeMap(
                his_code=code, drug_name=name, ingredient=ingr,
                status="SUGGESTED", confidence=s["confidence"],
                candidates=s["candidates"], matched_name=s.get("matched_name"),
                insurance_code=s.get("insurance_code")))   # HIGH는 보험코드 선반영(미확정)
            created += 1
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="regulatory.drug_code_map",
                                  after={"built": created})
        return {"scanned": len(rows), "created": created}

    async def confirm(self, session: AsyncSession, *, actor: int, his_code: str,
                      insurance_code: str) -> dict:
        """보험코드 확정 — fee_master 존재 검증 후 CONFIRMED. 이후 라인 자동 변환."""
        m = await session.scalar(select(DrugCodeMap).where(DrugCodeMap.his_code == his_code))
        if m is None:
            raise ValidationError("매핑 대상 아님(미등재 약품 빌드 먼저)")
        from erp.regulatory.models import FeeMaster
        ins = (await session.execute(select(FeeMaster.id, FeeMaster.name).where(
            FeeMaster.code == insurance_code, FeeMaster.code_type == "DRUG",
            FeeMaster.valid_to.is_(None)).limit(1))).first()
        if not ins:
            raise ValidationError(f"보험코드 미존재(약가 마스터): {insurance_code}")
        # H1: 제형군 일치 검증 — 정↔주사 등 오확정 방지(후보 단계 가드를 confirm도 적용)
        from erp.regulatory.drug_matcher import _form_group
        his_form = _form_group(m.drug_name or "")
        ins_form = _form_group(ins[1] or "")
        if his_form and ins_form and his_form != ins_form:
            raise ValidationError(
                f"제형 불일치 확정 차단: {m.drug_name}({his_form}) ↔ {ins[1]}({ins_form})")
        m.insurance_code = insurance_code
        m.status = "CONFIRMED"
        m.confirmed_by = actor
        m.updated_at = datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="regulatory.drug_code_map", target_id=his_code,
                                  after={"insurance_code": insurance_code, "status": "CONFIRMED"})
        return {"his_code": his_code, "insurance_code": insurance_code, "status": "CONFIRMED"}

    async def reject(self, session: AsyncSession, *, actor: int, his_code: str) -> dict:
        m = await session.scalar(select(DrugCodeMap).where(DrugCodeMap.his_code == his_code))
        if m is None:
            raise ValidationError("매핑 대상 아님")
        m.status = "REJECTED"
        m.updated_at = datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor, action="UPDATE",   # confirm/build과 대칭
                                  entity="regulatory.drug_code_map", target_id=his_code,
                                  after={"status": "REJECTED"})
        return {"his_code": his_code, "status": "REJECTED"}

    async def list_maps(self, session: AsyncSession, *, status: str | None = None,
                        page: int = 1, size: int = 50) -> dict:
        base = select(DrugCodeMap)
        if status:
            base = base.where(DrugCodeMap.status == status)
        total = await session.scalar(
            select(func.count()).select_from(base.subquery())) or 0
        rows = (await session.scalars(
            base.order_by(DrugCodeMap.confidence.desc(), DrugCodeMap.his_code)
            .offset((page - 1) * size).limit(size))).all()
        items = [{"his_code": m.his_code, "insurance_code": m.insurance_code,
                  "drug_name": m.drug_name, "ingredient": m.ingredient,
                  "status": m.status, "confidence": m.confidence,
                  "candidates": m.candidates, "matched_name": m.matched_name}
                 for m in rows]
        return {"items": items, "total": total, "page": page, "size": size}

    @staticmethod
    async def confirmed_map(session: AsyncSession) -> dict[str, str]:
        """CONFIRMED his_code→insurance_code (invoice 자동 변환용)."""
        rows = (await session.execute(text(
            "SELECT his_code, insurance_code FROM regulatory.drug_code_map "
            "WHERE status='CONFIRMED' AND insurance_code IS NOT NULL"))).all()
        return {hc: ic for hc, ic in rows}

    @staticmethod
    async def exempt_codes(session: AsyncSession) -> set[str]:
        """약가 비대상(혈액제제 등 — 혈액수가 체계) HIS 코드. 수가 마스터 미등재가 정상이므로
        사전심사 UNKNOWN_CODE 적발 제외용(drug_matcher가 matched_name으로 표식)."""
        rows = (await session.execute(text(
            "SELECT his_code FROM regulatory.drug_code_map "
            "WHERE matched_name LIKE '%약가 비대상%'"))).all()
        return {r[0] for r in rows}
