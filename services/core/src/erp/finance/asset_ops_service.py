"""고정자산 부서이동·실사(재물조사) — 내부운영 갭 감사 Tier2·마이그 0218.

audit 발견: FixedAsset.department_id UPDATE에 감사추적/이력 없음·물리 실사 미지원.
- 이동: 부서간 이동을 이력(AssetTransfer)으로 남기고 department_id 갱신(감사추적).
- 실사: 세션 개시 시 IN_USE 자산 스냅샷 → 라인별 FOUND/NOT_FOUND/RELOCATED 확인 → 마감·불일치 집계.
자산 원가/감가 전표는 불변(이동은 관리회계 cost_center 배부 대상, 자동 재분개는 미범위).
"""
from datetime import date, datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.errors import ValidationError
from erp.finance.models import (
    AssetInventory, AssetInventoryLine, AssetTransfer, FixedAsset,
)

_audit = AuditService()


class AssetOpsService:
    # ── 부서 이동 ───────────────────────────────────────────
    async def transfer(self, session: AsyncSession, *, asset_id: int, to_dept: int | None,
                       transferred_on: date, reason: str | None = None,
                       actor: int | None = None) -> AssetTransfer:
        asset = await session.get(FixedAsset, asset_id, with_for_update=True)
        if asset is None:
            raise ValidationError("존재하지 않는 자산입니다", {"asset_id": asset_id})
        if asset.status != "IN_USE":
            raise ValidationError("사용 중 자산만 이동할 수 있습니다", {"status": asset.status})
        if asset.department_id == to_dept:
            raise ValidationError("현재 부서와 동일합니다", {"department_id": to_dept})
        tr = AssetTransfer(asset_id=asset_id, from_dept=asset.department_id, to_dept=to_dept,
                           transferred_on=transferred_on, reason=reason, actor=actor)
        session.add(tr)
        asset.department_id = to_dept
        await session.flush()
        await _audit.record_audit(session, actor=actor or 0, action="TRANSFER",
                                  entity="finance.fixed_assets", target_id=str(asset_id),
                                  after={"from": tr.from_dept, "to": to_dept})
        return tr

    async def transfer_history(self, session: AsyncSession, *, asset_id: int) -> list[dict]:
        rows = (await session.scalars(
            select(AssetTransfer).where(AssetTransfer.asset_id == asset_id)
            .order_by(AssetTransfer.id))).all()
        return [{"id": t.id, "from_dept": t.from_dept, "to_dept": t.to_dept,
                 "transferred_on": t.transferred_on, "reason": t.reason, "actor": t.actor}
                for t in rows]

    # ── 실사(재물조사) ──────────────────────────────────────
    async def open_inventory(self, session: AsyncSession, *, name: str, conducted_on: date,
                             note: str | None = None,
                             actor: int | None = None) -> AssetInventory:
        """실사 개시 — 사용 중(IN_USE) 자산 전체를 라인으로 스냅샷(기대 부서).

        기관 전체 스냅샷이라 동시에 여러 OPEN 실사가 있으면 이중 집계·혼선. 진행 중(OPEN) 실사가
        있으면 개시 차단(중복 OPEN 가드·정합성 하드닝). 순수 동시 개시 경합은 저빈도·저영향(문서화)."""
        open_id = await session.scalar(
            select(AssetInventory.id).where(AssetInventory.status == "OPEN").limit(1))
        if open_id is not None:
            raise ValidationError("진행 중인 실사가 있습니다. 마감 후 개시하세요",
                                  {"open_inventory_id": open_id})
        inv = AssetInventory(name=name[:120], conducted_on=conducted_on, note=note,
                             created_by=actor, status="OPEN")
        session.add(inv)
        await session.flush()
        assets = (await session.execute(
            select(FixedAsset.id, FixedAsset.department_id)
            .where(FixedAsset.status == "IN_USE"))).all()
        for aid, dept in assets:
            session.add(AssetInventoryLine(inventory_id=inv.id, asset_id=aid,
                                           expected_dept=dept))
        await session.flush()
        return inv

    async def check_line(self, session: AsyncSession, *, line_id: int, found_status: str,
                         actual_dept: int | None = None, note: str | None = None,
                         actor: int | None = None) -> AssetInventoryLine:
        """실사 라인 확인 — FOUND(정위치)·NOT_FOUND(소재불명)·RELOCATED(타부서 발견)."""
        st = (found_status or "").upper()
        if st not in ("FOUND", "NOT_FOUND", "RELOCATED"):
            raise ValidationError("확인 상태 오류", {"found_status": found_status})
        line = await session.get(AssetInventoryLine, line_id)
        if line is None:
            raise ValidationError("존재하지 않는 실사 라인입니다", {"line_id": line_id})
        # 실사 헤더 행잠금 — close_inventory(동일 락)와 직렬화. 락 없이 status를 읽으면
        # OPEN 확인 후 close가 끼어들어 마감된 실사에 라인 기록되는 TOCTOU(정합성 하드닝).
        inv = await session.get(AssetInventory, line.inventory_id, with_for_update=True)
        if inv is None or inv.status != "OPEN":
            raise ValidationError("마감된 실사는 수정할 수 없습니다")
        if st == "RELOCATED" and actual_dept is None:
            raise ValidationError("이전(RELOCATED) 시 실제 부서가 필요합니다")
        line.found_status = st
        line.actual_dept = actual_dept if st == "RELOCATED" else None
        line.note = note
        line.checked_by = actor
        line.checked_at = datetime.now(timezone.utc)
        return line

    async def close_inventory(self, session: AsyncSession, *, inventory_id: int,
                              actor: int | None = None) -> dict:
        """실사 마감 — 결과 집계(정위치/소재불명/이전/미확인). 마감 후 라인 불변."""
        inv = await session.get(AssetInventory, inventory_id, with_for_update=True)
        if inv is None:
            raise ValidationError("존재하지 않는 실사입니다", {"inventory_id": inventory_id})
        if inv.status != "OPEN":
            raise ValidationError("이미 마감된 실사입니다")
        inv.status = "CLOSED"
        inv.closed_at = datetime.now(timezone.utc)
        summary = await self._summary(session, inventory_id)
        await _audit.record_audit(session, actor=actor or 0, action="CLOSE",
                                  entity="finance.asset_inventories", target_id=str(inventory_id),
                                  after=summary)
        return {"inventory_id": inventory_id, "status": inv.status, **summary}

    async def _summary(self, session: AsyncSession, inventory_id: int) -> dict:
        rows = (await session.execute(
            select(AssetInventoryLine.found_status, func.count())
            .where(AssetInventoryLine.inventory_id == inventory_id)
            .group_by(AssetInventoryLine.found_status))).all()
        counts = {s: int(n) for s, n in rows}
        return {"total": sum(counts.values()),
                "found": counts.get("FOUND", 0), "not_found": counts.get("NOT_FOUND", 0),
                "relocated": counts.get("RELOCATED", 0),
                "not_checked": counts.get("NOT_CHECKED", 0)}

    async def inventory_detail(self, session: AsyncSession, *, inventory_id: int,
                               discrepancy_only: bool = False) -> dict:
        inv = await session.get(AssetInventory, inventory_id)
        if inv is None:
            raise ValidationError("존재하지 않는 실사입니다", {"inventory_id": inventory_id})
        q = select(AssetInventoryLine).where(AssetInventoryLine.inventory_id == inventory_id)
        if discrepancy_only:
            q = q.where(AssetInventoryLine.found_status.in_(
                ("NOT_FOUND", "RELOCATED", "NOT_CHECKED")))
        lines = (await session.scalars(q.order_by(AssetInventoryLine.id))).all()
        return {"id": inv.id, "name": inv.name, "conducted_on": inv.conducted_on,
                "status": inv.status, "summary": await self._summary(session, inventory_id),
                "lines": [{"id": ln.id, "asset_id": ln.asset_id,
                           "expected_dept": ln.expected_dept, "found_status": ln.found_status,
                           "actual_dept": ln.actual_dept, "note": ln.note} for ln in lines]}

    async def list_inventories(self, session: AsyncSession) -> list[dict]:
        rows = (await session.scalars(
            select(AssetInventory).order_by(AssetInventory.id.desc()).limit(200))).all()
        return [{"id": i.id, "name": i.name, "conducted_on": i.conducted_on,
                 "status": i.status} for i in rows]
