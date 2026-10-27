"""사전심사 학습 사례 서비스 — 결정된 적발을 라벨된 AI 학습 데이터로 등록·관리.

자동 수확(human 결정 적발) + 수동 등록 → 품질 승인·의료진 확인분만 학습 적격(eligible)
→ JSONL 내보내기. label=REDUCTION(삭감위험·인정)|FALSE_POSITIVE(오탐).
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.claims.models import (
    PreReviewFinding, PreReviewLearningCase, PreReviewQuery, PreReviewRule,
)
from erp.errors import ValidationError

_audit = AuditService()
_LABEL = {"ACCEPT": "REDUCTION", "DISMISS": "FALSE_POSITIVE"}


class LearningCaseService:
    async def _gated_codes(self, session: AsyncSession) -> set[str]:
        return set((await session.scalars(
            select(PreReviewRule.code).where(PreReviewRule.physician_gated.is_(True)))).all())

    async def _rule_types(self, session: AsyncSession) -> dict[str, str]:
        rows = (await session.execute(
            select(PreReviewRule.code, PreReviewRule.rule_type))).all()
        return {c: t for c, t in rows}

    def _eligible(self, lc: PreReviewLearningCase, gated: set[str]) -> bool:
        """AI 학습 적격 — 승인·활성·라벨, 의학판단 룰은 의료진 확인 필수.

        근거(rationale)는 권장이나 필수 아님(인정=삭감위험은 사유 미강제·라벨이 핵심 신호.
        오탐=DISMISS는 사유 강제라 자연히 존재). 오탐은 근거가 있어야 학습 신뢰도↑.
        """
        if not lc.active or lc.quality != "APPROVED":
            return False
        if lc.label == "FALSE_POSITIVE" and not (lc.rationale or "").strip():
            return False   # 오탐은 사유(왜 오탐인지)가 학습 컨텍스트 — 없으면 제외
        if lc.rule_code in gated and not lc.physician_confirmed:
            return False
        return True

    async def harvest(self, session: AsyncSession, *, actor: int, limit: int = 500) -> dict:
        """결정된 human 적발(인정/오탐)을 학습 사례로 자동 수확 — source_finding_id 멱등.

        의료진 확인분은 즉시 APPROVED(고품질 라벨), 그 외 DRAFT(검토 후 승인).
        auto_decided(P2 자동오탐)은 제외 — 자동→자동 순환 학습 방지.
        """
        existing = set((await session.scalars(
            select(PreReviewLearningCase.source_finding_id)
            .where(PreReviewLearningCase.source_finding_id.isnot(None)))).all())
        findings = (await session.scalars(
            select(PreReviewFinding).where(
                PreReviewFinding.decision.in_(("ACCEPT", "DISMISS")),
                PreReviewFinding.auto_decided.is_(False))
            .order_by(PreReviewFinding.decided_at.desc()).limit(limit))).all()
        from erp.claims.models import PreReviewCase
        rtypes = await self._rule_types(session)
        # N+1 제거 — 신규 대상 case·의료진확인질의를 일괄 로드(건당 단건조회 → 2 일괄조회)
        fresh = [f for f in findings if f.id not in existing]
        case_ids = {f.case_id for f in fresh}
        cases = {c.id: c for c in (await session.scalars(
            select(PreReviewCase).where(PreReviewCase.id.in_(case_ids)))).all()} if case_ids else {}
        fids = {f.id for f in fresh}
        conf_by_finding: dict[int, int] = {}
        if fids:
            for fid, emp in (await session.execute(
                    select(PreReviewQuery.finding_id, PreReviewQuery.confirmed_by_employee).where(
                        PreReviewQuery.finding_id.in_(fids),
                        PreReviewQuery.confirmed_by_employee.isnot(None)))).all():
                conf_by_finding.setdefault(fid, emp)   # 임의 1건(확인 여부만 필요)
        added = 0
        for f in fresh:
            case = cases.get(f.case_id)
            conf = conf_by_finding.get(f.id)
            rtype = rtypes.get(f.rule_code)
            lc = PreReviewLearningCase(
                source_finding_id=f.id,
                chart_no=case.chart_no if case else None,
                dept_code=case.dept_code if case else None,   # 부서경계(IDOR/PII)
                care_month=case.care_month if case else None,
                encounter_type=case.encounter_type if case else None,
                rule_code=f.rule_code, rule_type=rtype, code=f.code,
                amount=float(f.amount or 0),
                label=_LABEL[f.decision], rationale=f.decision_note,
                physician_confirmed=conf is not None, confirmed_by_employee=conf,
                features={"rule_type": rtype, "rule_code": f.rule_code, "code": f.code,
                          "amount": float(f.amount or 0), "severity": f.severity,
                          "encounter_type": case.encounter_type if case else None},
                quality="APPROVED" if conf is not None else "DRAFT",
                source="AUTO", created_by=actor)
            session.add(lc)
            added += 1
        await _audit.record_audit(session, actor=actor, action="HARVEST",
                                  entity="claims.pre_review_learning_cases",
                                  target_id="batch", after={"added": added})
        return {"added": added, "scanned": len(findings)}

    async def create(self, session: AsyncSession, *, actor: int, label: str,
                     rationale: str | None = None, source_finding_id: int | None = None,
                     code: str | None = None, rule_code: str | None = None,
                     tags: str | None = None) -> PreReviewLearningCase:
        """수동 학습 사례 등록 — 적발(source_finding_id) 또는 자유 입력."""
        if label not in ("REDUCTION", "FALSE_POSITIVE"):
            raise ValidationError("label은 REDUCTION|FALSE_POSITIVE")
        chart = month = enc = dx = rtype = dept = None
        amount = 0.0
        if source_finding_id is not None:
            dup = await session.scalar(select(PreReviewLearningCase.id).where(
                PreReviewLearningCase.source_finding_id == source_finding_id))
            if dup:
                raise ValidationError("이미 등록된 적발입니다")
            f = await session.get(PreReviewFinding, source_finding_id)
            if f is None:
                raise ValidationError("존재하지 않는 적발입니다")
            from erp.claims.models import PreReviewCase
            case = await session.get(PreReviewCase, f.case_id)
            chart = case.chart_no if case else None
            dept = case.dept_code if case else None       # 부서경계(IDOR/PII)
            month = case.care_month if case else None
            enc = case.encounter_type if case else None
            code = code or f.code
            rule_code = rule_code or f.rule_code
            amount = float(f.amount or 0)
            rtype = (await self._rule_types(session)).get(rule_code)
        lc = PreReviewLearningCase(
            source_finding_id=source_finding_id, chart_no=chart, dept_code=dept,
            care_month=month,
            encounter_type=enc, rule_code=rule_code, rule_type=rtype, code=code,
            diagnosis_codes=dx, amount=amount, label=label,
            rationale=(rationale or "").strip() or None, tags=tags,
            features={"rule_type": rtype, "rule_code": rule_code, "code": code, "amount": amount},
            quality="DRAFT", source="MANUAL", created_by=actor)
        session.add(lc)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="claims.pre_review_learning_cases",
                                  target_id=str(lc.id), after={"label": label, "source": "MANUAL"})
        return lc

    async def update(self, session: AsyncSession, *, actor: int, case_id: int,
                     label: str | None = None, rationale: str | None = None,
                     quality: str | None = None, tags: str | None = None,
                     active: bool | None = None) -> PreReviewLearningCase:
        lc = await session.get(PreReviewLearningCase, case_id)
        if lc is None:
            raise ValidationError("존재하지 않는 학습 사례입니다")
        if label is not None:
            if label not in ("REDUCTION", "FALSE_POSITIVE"):
                raise ValidationError("label은 REDUCTION|FALSE_POSITIVE")
            lc.label = label
        if quality is not None:
            if quality not in ("DRAFT", "APPROVED", "REJECTED"):
                raise ValidationError("quality는 DRAFT|APPROVED|REJECTED")
            lc.quality = quality
        if rationale is not None:
            lc.rationale = rationale.strip() or None
        if tags is not None:
            lc.tags = tags[:200] or None
        if active is not None:
            lc.active = active
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="claims.pre_review_learning_cases",
                                  target_id=str(case_id),
                                  after={"label": lc.label, "quality": lc.quality})
        return lc

    async def list_cases(self, session: AsyncSession, *, label: str | None = None,
                         quality: str | None = None, source: str | None = None,
                         eligible_only: bool = False, limit: int = 500,
                         dept_codes: list[str] | None = None) -> dict:
        q = select(PreReviewLearningCase).where(PreReviewLearningCase.active.is_(True))
        if dept_codes is not None:   # 부서경계(IDOR/PII) — 본부서만(dept_code NULL 레거시는 ADMIN 전용)
            q = q.where(PreReviewLearningCase.dept_code.in_(dept_codes))
        if label:
            q = q.where(PreReviewLearningCase.label == label)
        if quality:
            q = q.where(PreReviewLearningCase.quality == quality)
        if source:
            q = q.where(PreReviewLearningCase.source == source)
        rows = (await session.scalars(
            q.order_by(PreReviewLearningCase.id.desc()).limit(limit))).all()
        gated = await self._gated_codes(session)
        items = [self._dto(lc, gated) for lc in rows]
        if eligible_only:
            items = [i for i in items if i["eligible"]]
        return {"items": items,
                "summary": {"total": len(rows),
                            "eligible": sum(1 for i in items if i["eligible"]),
                            "reduction": sum(1 for i in items if i["label"] == "REDUCTION"),
                            "false_positive": sum(1 for i in items if i["label"] == "FALSE_POSITIVE")}}

    def _dto(self, lc: PreReviewLearningCase, gated: set[str]) -> dict:
        return {"id": lc.id, "source_finding_id": lc.source_finding_id,
                "chart_no": lc.chart_no, "dept_code": lc.dept_code, "care_month": lc.care_month,
                "rule_code": lc.rule_code, "rule_type": lc.rule_type, "code": lc.code,
                "amount": float(lc.amount or 0), "label": lc.label, "rationale": lc.rationale,
                "physician_confirmed": lc.physician_confirmed, "tags": lc.tags,
                "quality": lc.quality, "source": lc.source,
                "eligible": self._eligible(lc, gated)}

    async def export(self, session: AsyncSession, *,
                     dept_codes: list[str] | None = None) -> list[dict]:
        """학습 적격 사례를 학습 레코드(features+label)로 — JSONL 내보내기용."""
        gated = await self._gated_codes(session)
        q = select(PreReviewLearningCase).where(
            PreReviewLearningCase.active.is_(True),
            PreReviewLearningCase.quality == "APPROVED")
        if dept_codes is not None:   # 부서경계 — 반출도 본부서만(NULL 레거시 ADMIN 전용)
            q = q.where(PreReviewLearningCase.dept_code.in_(dept_codes))
        rows = (await session.scalars(q)).all()
        out = []
        for lc in rows:
            if not self._eligible(lc, gated):
                continue
            out.append({"id": lc.id, "label": lc.label,
                        "features": lc.features or {}, "rationale": lc.rationale,
                        "physician_confirmed": lc.physician_confirmed,
                        "rule_code": lc.rule_code, "code": lc.code,
                        "amount": float(lc.amount or 0)})
        return out
