"""ITAM 서비스 — IT 자산 대장·배정·벤더 계약 (M1)."""
import csv
import io
import json
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common.approval_engine import ApprovalEngine
from erp.errors import ValidationError
from erp.hr.models import Employee
from erp.itam.models import (
    ASSET_STATUSES, CONTRACT_KINDS, AssetAssignment, ITAsset, MaintenanceRecord, Offboarding,
    ServiceAllocation, SoftwareService, VendorContract,
)

_audit = AuditService()
_approval = ApprovalEngine()
_DISPOSAL_DOC = "ITAM_DISPOSAL"     # 폐기 결재 subject(F-3) — 규칙 있으면 결재, 없으면 기존 직접폐기
MAINTENANCE_KINDS = ("REPAIR", "INSPECTION", "UPGRADE", "CALIBRATION")
# 생성 시 허용 상태 — ASSIGNED(배정 이력 필요)·DISPOSED(소거 게이트)는 전용 경로만 (검토 S-1)
_CREATE_BLOCKED_STATUSES = ("ASSIGNED", "DISPOSED")
_MAX_SPEC_BYTES = 8192          # 장비 디테일 JSON 상한 (검토 S-4)


def like_pattern(q: str) -> str:
    """ilike 검색어의 와일드카드 이스케이프(%·_) — 전건 매칭·중첩 패턴 스캔 방지."""
    return f"%{q.strip().replace(chr(92), chr(92) * 2).replace('%', chr(92) + '%').replace('_', chr(92) + '_')}%"


class ITAMService:
    # ── 벤더 계약 ─────────────────────────────────────────────
    # 계약 관리 필드(vendor·title·kind는 별도) — 생성/수정 공통
    _CONTRACT_FIELDS = ("signed_on", "start_on", "end_on", "next_review_on", "amount",
                        "counterparty", "counterparty_contact", "owner_employee_id",
                        "department_id", "reason", "sla_note", "note")

    async def create_contract(self, session: AsyncSession, *, actor: int, vendor: str,
                              title: str, kind: str = "PURCHASE", **fields) -> VendorContract:
        if kind not in CONTRACT_KINDS:
            raise ValidationError(f"계약 유형 오류: {kind}")
        if not vendor.strip() or not title.strip():
            raise ValidationError("벤더·계약명은 필수입니다")
        c = VendorContract(vendor=vendor.strip(), title=title.strip(), kind=kind, created_by=actor,
                           **{k: fields.get(k) for k in self._CONTRACT_FIELDS})
        session.add(c)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="itam.vendor_contracts", target_id=str(c.id),
                                  after={"vendor": vendor, "kind": kind})
        return c

    async def get_contract(self, session: AsyncSession, contract_id: int) -> VendorContract:
        c = await session.get(VendorContract, contract_id)
        if c is None:
            raise ValidationError("존재하지 않는 계약입니다")
        return c

    async def update_contract(self, session: AsyncSession, *, actor: int, contract_id: int,
                              **fields) -> VendorContract:
        c = await self.get_contract(session, contract_id)
        if fields.get("kind") and fields["kind"] not in CONTRACT_KINDS:
            raise ValidationError(f"계약 유형 오류: {fields['kind']}")
        for req in ("vendor", "title"):
            if req in fields and not (fields[req] or "").strip():
                raise ValidationError("벤더·계약명은 필수입니다")
        before = {k: getattr(c, k) for k in fields if hasattr(c, k)}
        for k, v in fields.items():
            if hasattr(c, k) and k not in ("id", "created_by", "created_at"):
                setattr(c, k, v.strip() if isinstance(v, str) and k in ("vendor", "title") else v)
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="itam.vendor_contracts", target_id=str(c.id),
                                  before=before, after=fields)
        return c

    async def list_contracts(self, session: AsyncSession) -> list[VendorContract]:
        return list((await session.scalars(
            select(VendorContract).order_by(VendorContract.id.desc()))).all())

    # ── 자산 대장 ─────────────────────────────────────────────
    async def create_asset(self, session: AsyncSession, *, actor: int, asset_tag: str,
                           name: str, category: str = "LAPTOP", **fields) -> ITAsset:
        tag = asset_tag.strip()
        if not tag or not name.strip():
            raise ValidationError("자산 태그·명칭은 필수입니다")
        if await session.scalar(select(ITAsset.id).where(ITAsset.asset_tag == tag)):
            raise ValidationError(f"이미 존재하는 자산 태그: {tag}")
        serial = (fields.get("serial_no") or "").strip() or None
        if serial and await session.scalar(select(ITAsset.id).where(ITAsset.serial_no == serial)):
            raise ValidationError(f"이미 등록된 시리얼: {serial}")
        status = fields.get("status", "IN_STOCK")
        if status not in ASSET_STATUSES:
            raise ValidationError(f"상태 오류: {status}")
        if status in _CREATE_BLOCKED_STATUSES:   # S-1 — 생성 시 배정/폐기 상태 차단
            raise ValidationError(f"'{status}' 상태로는 등록할 수 없습니다(배정은 배정 기능·폐기는 폐기 처리)")
        if fields.get("spec") and len(json.dumps(fields["spec"])) > _MAX_SPEC_BYTES:
            raise ValidationError("장비 디테일(spec)이 너무 큽니다")
        phi = bool(fields.get("handles_phi"))
        a = ITAsset(asset_tag=tag, name=name.strip(), category=category, status=status,
                    created_by=actor, serial_no=serial, phi_ever=phi,   # S-2 sticky
                    **{k: v for k, v in fields.items() if k not in ("serial_no", "status")})
        session.add(a)
        try:
            await session.flush()
        except IntegrityError as e:   # 재검토 — 동시 등록 태그/시리얼 충돌(TOCTOU)을 500 대신 400으로
            raise ValidationError("자산 태그 또는 시리얼이 방금 등록되었습니다 — 다시 시도하세요") from e
        await _audit.record_audit(session, actor=actor, action="CREATE", entity="itam.it_assets",
                                  target_id=str(a.id), after={"asset_tag": tag, "name": name})
        return a

    async def get_asset(self, session: AsyncSession, asset_id: int) -> ITAsset:
        a = await session.get(ITAsset, asset_id)
        if a is None:
            raise ValidationError("존재하지 않는 자산입니다")
        return a

    # ── IT자산 리스 취득(0227) — IFRS16 리스계약 생성·연결 ────────
    async def attach_lease(self, session: AsyncSession, *, actor: int, asset_id: int,
                           start_on: date, term_months: int, monthly_payment: float,
                           annual_rate: float = 0, lessor: str = "", exempt: bool = False,
                           lease_no: str | None = None) -> dict:
        """IT자산에 IFRS16 리스계약을 개시·연결. 이미 리스/구매 취득이면 거부(상호 배타)."""
        from erp.finance.lease_service import LeaseService
        from erp.finance.models import LeaseContract
        a = await self.get_asset(session, asset_id)
        if a.lease_id is not None:
            raise ValidationError("이미 리스가 연결된 자산입니다", {"lease_id": a.lease_id})
        if a.finance_asset_id is not None:
            raise ValidationError("구매(자체 취득) 자산에는 리스를 연결할 수 없습니다")
        no = (lease_no or "").strip() or f"ITL-{a.asset_tag}"
        if await session.scalar(select(LeaseContract.id).where(LeaseContract.lease_no == no)):
            raise ValidationError(f"이미 존재하는 리스번호: {no}")
        lease = await LeaseService().create(
            session, lease_no=no, asset_name=f"{a.name} ({a.asset_tag})", start_on=start_on,
            term_months=term_months, monthly_payment=monthly_payment, annual_rate=annual_rate,
            lessor=lessor, exempt=exempt, asset_kind="IT", itam_asset_id=a.id, user_id=actor)
        a.acquisition_type, a.lease_id = "LEASED", lease.id
        if a.acquired_on is None:
            a.acquired_on = start_on
        await _audit.record_audit(session, actor=actor, action="ATTACH_LEASE",
                                  entity="itam.it_assets", target_id=str(a.id),
                                  after={"lease_id": lease.id, "lease_no": no,
                                         "monthly": monthly_payment, "term": term_months})
        return {"asset_id": a.id, "lease_id": lease.id, "lease_no": no,
                "rou_initial": float(lease.rou_initial),
                "liability_initial": float(lease.liability_initial), "exempt": lease.exempt}

    async def lease_summary(self, session: AsyncSession, asset_id: int) -> dict | None:
        """자산에 연결된 리스 요약(상세 화면용) — 미연결이면 None."""
        a = await self.get_asset(session, asset_id)
        if a.lease_id is None:
            return None
        from erp.finance.models import LeaseContract
        x = await session.get(LeaseContract, a.lease_id)
        if x is None:
            return None
        return {"lease_id": x.id, "lease_no": x.lease_no, "lessor": x.lessor,
                "start_on": x.start_on, "term_months": x.term_months,
                "monthly_payment": float(x.monthly_payment), "annual_rate": float(x.annual_rate),
                "rou_book": float(x.rou_initial) - float(x.accumulated_amort),
                "current_liability": float(x.current_liability),
                "periods_accrued": x.periods_accrued, "exempt": x.exempt, "status": x.status}

    # ── 조달 PO 연계(R4) — 입고된 EQUIPMENT를 IT 자산으로 등록 ──
    async def procurement_candidates(self, session: AsyncSession) -> list[dict]:
        """입고된 구매발주(EQUIPMENT) 라인 중 IT 자산 미등록(잔여>0) — 조달 연계 등록 후보.

        scm 조달 데이터를 읽어(풀) IT 담당자가 자산으로 등록. 라인당 입고수량−등록수량>0만 노출.
        """
        from erp.scm.models import Item, PoLine, PurchaseOrder, Supplier
        rows = (await session.execute(
            select(PoLine, Item.name, Item.code, Supplier.name,
                   PurchaseOrder.department_id, PurchaseOrder.received_at)
            .join(Item, Item.id == PoLine.item_id)
            .join(PurchaseOrder, PurchaseOrder.id == PoLine.po_id)
            .join(Supplier, Supplier.id == PurchaseOrder.supplier_id)
            .where(Item.category == "EQUIPMENT", PoLine.received_qty > 0)
            .order_by(PoLine.id.desc()))).all()
        line_ids = [pl.id for pl, *_ in rows]
        created: dict[int, int] = {}
        if line_ids:
            created = dict((await session.execute(
                select(ITAsset.procurement_po_line_id, func.count())
                .where(ITAsset.procurement_po_line_id.in_(line_ids))
                .group_by(ITAsset.procurement_po_line_id))).all())
        out = []
        for pl, item_name, item_code, supplier, dept_id, received_at in rows:
            reg = created.get(pl.id, 0)
            remaining = int(float(pl.received_qty)) - reg
            if remaining <= 0:
                continue
            out.append({"po_line_id": pl.id, "po_id": pl.po_id, "item_code": item_code,
                        "item_name": item_name, "supplier": supplier,
                        "unit_price": float(pl.unit_price), "received_qty": float(pl.received_qty),
                        "registered": reg, "remaining": remaining, "department_id": dept_id,
                        "received_at": received_at})
        return out

    async def register_from_po(self, session: AsyncSession, *, actor: int, po_line_id: int,
                               asset_tag: str | None = None, category: str = "PERIPHERAL",
                               serial_no: str | None = None, name: str | None = None,
                               **fields) -> ITAsset:
        """입고 PO 라인 → IT 자산 등록(품목명·공급사·도입일·부서 프리필·po 링크). 잔여 초과 차단."""
        from erp.scm.models import Item, PoLine, PurchaseOrder, Supplier
        # 재검토 MED — 행 잠금으로 동시 등록 경합(입고수량 초과 등록) 직렬화
        pl = await session.scalar(select(PoLine).where(PoLine.id == po_line_id).with_for_update())
        if pl is None:
            raise ValidationError("입고 라인이 없습니다")
        item = await session.get(Item, pl.item_id)
        if item is None or item.category != "EQUIPMENT":
            raise ValidationError("EQUIPMENT(장비) 품목만 IT 자산으로 등록할 수 있습니다")
        if float(pl.received_qty) <= 0:
            raise ValidationError("입고 수량이 없습니다")
        reg = await session.scalar(select(func.count()).select_from(ITAsset)
                                   .where(ITAsset.procurement_po_line_id == po_line_id)) or 0
        if reg >= int(float(pl.received_qty)):
            raise ValidationError("입고 수량만큼 이미 자산으로 등록되었습니다")
        po = await session.get(PurchaseOrder, pl.po_id)
        supplier = await session.get(Supplier, po.supplier_id) if po else None
        tag = (asset_tag or "").strip() or await self.suggest_tag(session)
        acquired = po.received_at.date() if po and po.received_at else None
        return await self.create_asset(
            session, actor=actor, asset_tag=tag, name=(name or item.name), category=category,
            serial_no=serial_no, manufacturer=(supplier.name if supplier else None),
            acquired_on=acquired, owner_department_id=(po.department_id if po else None),
            procurement_po_line_id=po_line_id,
            note=f"조달 입고 연계 — 발주 #{pl.po_id} · 품목 {item.code}", **fields)

    async def suggest_tag(self, session: AsyncSession, prefix: str = "IT") -> str:
        """QR 자산 태그 자동 생성 — 기존 '{prefix}-NNNNNN' 중 최대 번호+1(6자리). 등록 폼 편의."""
        pfx = (prefix or "IT").strip().upper()[:12] or "IT"
        tags = (await session.scalars(select(ITAsset.asset_tag).where(
            ITAsset.asset_tag.like(f"{pfx}-%")))).all()
        mx = 0
        for t in tags:
            suf = t.rsplit("-", 1)[-1]
            if suf.isdigit():
                mx = max(mx, int(suf))
        return f"{pfx}-{mx + 1:06d}"

    async def lookup_by_tag(self, session: AsyncSession, tag: str) -> ITAsset | None:
        """QR/바코드 스캔 조회 — 태그로 자산 1건(대소문자 무시 — 스캐너/수기 변형 대응)."""
        return await session.scalar(select(ITAsset).where(
            func.upper(ITAsset.asset_tag) == tag.strip().upper()))

    # ── 실사 세션(F-9) — QR 현장 실사 확인 시각 스탬프 + 미실사 리포트 ──
    async def audit_stamp(self, session: AsyncSession, *, actor: int, asset_id: int,
                          location: str | None = None) -> ITAsset:
        """현장 실사 확인 — last_audited_at 스탬프(+위치 갱신). 전수 실사 입증 증적."""
        a = await self.get_asset(session, asset_id)
        if a.status == "DISPOSED":
            raise ValidationError("폐기된 자산은 실사 대상이 아닙니다")
        a.last_audited_at = datetime.now(timezone.utc)
        if location is not None and location.strip():
            a.location = location.strip()
        await _audit.record_audit(session, actor=actor, action="AUDIT", entity="itam.it_assets",
                                  target_id=str(asset_id),
                                  after={"last_audited_at": a.last_audited_at.isoformat(),
                                         "location": a.location})
        return a

    async def audit_report(self, session: AsyncSession, *, days: int = 180) -> dict:
        """미실사 자산 리포트 — 기준일수 내 실사 기록이 없는(또는 전무한) 운영 자산."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
        active = select(ITAsset).where(ITAsset.status != "DISPOSED")
        total = int(await session.scalar(
            select(func.count()).select_from(active.subquery())) or 0)
        stale = (await session.scalars(active.where(
            or_(ITAsset.last_audited_at.is_(None), ITAsset.last_audited_at < cutoff))
            .order_by(ITAsset.last_audited_at.is_(None).desc(),
                      ITAsset.last_audited_at.asc()).limit(500))).all()
        return {"days": days, "total_active": total, "unaudited": len(stale),
                "items": [{"id": a.id, "asset_tag": a.asset_tag, "name": a.name,
                           "category": a.category, "status": a.status, "location": a.location,
                           "last_audited_at": a.last_audited_at} for a in stale]}

    async def list_assets(self, session: AsyncSession, *, category: str | None = None,
                          status: str | None = None, department_id: int | None = None,
                          handles_phi: bool | None = None, assigned: bool | None = None,
                          q: str | None = None) -> list[ITAsset]:
        stmt = select(ITAsset)
        if category:
            stmt = stmt.where(ITAsset.category == category)
        if status:
            stmt = stmt.where(ITAsset.status == status)
        if department_id is not None:
            stmt = stmt.where(ITAsset.owner_department_id == department_id)
        if handles_phi is not None:
            stmt = stmt.where(ITAsset.handles_phi == handles_phi)
        if assigned is not None:
            stmt = stmt.where(ITAsset.assigned_employee_id.isnot(None) if assigned
                              else ITAsset.assigned_employee_id.is_(None))
        if q:
            like = like_pattern(q)
            stmt = stmt.where(or_(ITAsset.name.ilike(like, escape=chr(92)),
                                  ITAsset.asset_tag.ilike(like, escape=chr(92)),
                                  ITAsset.serial_no.ilike(like, escape=chr(92))))
        return list((await session.scalars(stmt.order_by(ITAsset.id.desc()).limit(500))).all())

    # patch 전이 시 open 배정을 자동 마감하는 비배정 상태(유령 배정 방지)
    _UNASSIGN_STATUSES = ("IN_STOCK", "RETIRED", "LOST", "STOLEN")

    async def update_asset(self, session: AsyncSession, *, actor: int, asset_id: int,
                           **fields) -> ITAsset:
        a = await self.get_asset(session, asset_id)
        new_status = fields.get("status")
        if new_status is not None:
            if new_status not in ASSET_STATUSES:
                raise ValidationError(f"상태 오류: {new_status}")
            if new_status != a.status:
                # 전이 가드 — 폐기(PHI 소거 게이트)·배정은 전용 경로만 (검토 H-1)
                if a.status == "DISPOSED":
                    raise ValidationError("폐기된 자산은 상태를 변경할 수 없습니다")
                if new_status == "DISPOSED":
                    raise ValidationError("폐기는 [폐기 처리]로만 가능합니다(데이터 소거 확인 게이트)")
                if new_status == "ASSIGNED":
                    raise ValidationError("배정 상태는 배정 기능으로만 설정할 수 있습니다")
                if new_status in self._UNASSIGN_STATUSES and a.assigned_employee_id:
                    # 비배정 상태 전이 → open 배정 자동 마감(분실·도난은 사유 기록)
                    cond = "분실·도난 처리" if new_status in ("LOST", "STOLEN") else None
                    await self._close_open(session, asset_id, on=date.today(), condition=cond)
                    a.assigned_employee_id = None
        if fields.get("spec") and len(json.dumps(fields["spec"])) > _MAX_SPEC_BYTES:
            raise ValidationError("장비 디테일(spec)이 너무 큽니다")
        if fields.get("handles_phi"):     # S-2 — PHI 지정은 sticky(이후 False로 내려도 게이트 유지)
            a.phi_ever = True
        # before를 변경 필드와 대칭 수집(보안 속성 이전값 추적 — 검토 L-4)
        before = {k: getattr(a, k) for k in fields if hasattr(a, k)}
        for k, v in fields.items():
            if hasattr(a, k) and k not in ("id", "asset_tag", "created_by", "created_at", "phi_ever"):
                setattr(a, k, v)
        await _audit.record_audit(session, actor=actor, action="UPDATE", entity="itam.it_assets",
                                  target_id=str(a.id), before=before, after=fields)
        return a

    # 일괄 upsert 대상 필드(asset_tag는 키·별도)
    _BULK_FIELDS = ("name", "category", "serial_no", "manufacturer", "model", "status",
                    "location", "warranty_until", "ip_addr", "hostname", "handles_phi")

    async def bulk_upsert(self, session: AsyncSession, *, actor: int,
                          rows: list[dict]) -> dict:
        """스프레드시트 일괄 저장 — asset_tag 키로 upsert(신규 생성·기존 갱신). 행 단위 savepoint 격리.

        태그·이름 없는 행은 건너뜀. 각 행 실패는 격리해 나머지 진행(errors 수집).
        """
        if len(rows) > 2000:      # S-4 — 배치 크기 상한(워커 점유 방지)
            raise ValidationError("일괄 저장은 2,000행 이하로 나눠 주세요")
        created = updated = skipped = 0
        errors: list[dict] = []
        for i, r in enumerate(rows):
            tag = (r.get("asset_tag") or "").strip()
            name = (r.get("name") or "").strip()
            if not tag or not name:
                skipped += 1
                continue
            fields = {k: r[k] for k in self._BULK_FIELDS
                      if r.get(k) is not None and r.get(k) != ""}
            try:
                async with session.begin_nested():
                    existing = await session.scalar(
                        select(ITAsset).where(ITAsset.asset_tag == tag))
                    if existing:
                        await self.update_asset(session, actor=actor, asset_id=existing.id,
                                                **{k: v for k, v in fields.items() if k != "name"},
                                                name=name)
                        updated += 1
                    else:
                        await self.create_asset(session, actor=actor, asset_tag=tag, **fields)
                        created += 1
            except Exception as e:   # noqa: BLE001 — 행 격리(전체 배치 보호)
                # 도메인 오류만 원문 통과 — DB 예외 원문(SQL·스키마)은 응답에 노출하지 않음(검토 L-1)
                msg = str(e) if isinstance(e, ValidationError) \
                    else "저장 실패(시리얼 중복 등 제약 위반)"
                errors.append({"row": i, "asset_tag": tag, "error": msg})
        return {"created": created, "updated": updated, "skipped": skipped,
                "saved": created + updated, "errors": errors}

    # ── 배정/반납 ─────────────────────────────────────────────
    async def assign(self, session: AsyncSession, *, actor: int, asset_id: int, employee_id: int,
                     assigned_on: date | None = None, note: str | None = None) -> AssetAssignment:
        a = await self.get_asset(session, asset_id)
        if a.status in ("RETIRED", "DISPOSED", "LOST", "STOLEN"):
            raise ValidationError(f"배정할 수 없는 상태입니다 ({a.status})")
        if a.status == "REPAIR":
            raise ValidationError("수리 중 자산은 배정할 수 없습니다(수리 완료 후 상태 변경)")
        emp = await session.get(Employee, employee_id)
        if emp is None:
            raise ValidationError("존재하지 않는 직원입니다")
        if emp.resigned_on is not None:
            raise ValidationError("퇴직한 직원에게는 배정할 수 없습니다")
        # 기존 open 배정 자동 반납(사용자 교체)
        await self._close_open(session, asset_id, on=assigned_on or date.today())
        asn = AssetAssignment(asset_id=asset_id, employee_id=employee_id,
                              assigned_on=assigned_on or date.today(), assigned_by=actor, note=note)
        session.add(asn)
        a.assigned_employee_id = employee_id
        if a.status != "LOANER":     # 대여 자산은 LOANER 유지(풀 표식) — 사용자만 기록
            a.status = "ASSIGNED"
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="ASSIGN", entity="itam.it_assets",
                                  target_id=str(asset_id), after={"employee_id": employee_id})
        return asn

    async def return_asset(self, session: AsyncSession, *, actor: int, asset_id: int,
                           on: date | None = None, condition: str | None = None) -> None:
        a = await self.get_asset(session, asset_id)
        await self._close_open(session, asset_id, on=on or date.today(), condition=condition)
        a.assigned_employee_id = None
        if a.status == "ASSIGNED":
            a.status = "IN_STOCK"
        await _audit.record_audit(session, actor=actor, action="RETURN", entity="itam.it_assets",
                                  target_id=str(asset_id), after={"returned_on": str(on or date.today())})

    async def acknowledge(self, session: AsyncSession, *, actor: int, assignment_id: int,
                          employee_id: int | None = None) -> None:
        """수령 확인(chain-of-custody) — open 배정에 확인 시각 기록. 기확인·기반납 거부·감사.

        employee_id 지정 시(본인 확인 경로·F-7) 배정 당사자 일치 검증 — 대리 확인 차단.
        """
        asn = await session.get(AssetAssignment, assignment_id)
        if asn is None:
            raise ValidationError("배정 기록 없음")
        if employee_id is not None and asn.employee_id != employee_id:
            raise ValidationError("본인에게 배정된 자산만 수령 확인할 수 있습니다")
        if asn.returned_on is not None:
            raise ValidationError("이미 반납된 배정입니다")
        if asn.acknowledged_at is not None:
            raise ValidationError("이미 수령 확인된 배정입니다")
        asn.acknowledged_at = datetime.now(timezone.utc)
        await _audit.record_audit(session, actor=actor,
                                  action="ACKNOWLEDGE_SELF" if employee_id is not None else "ACKNOWLEDGE",
                                  entity="itam.asset_assignments", target_id=str(assignment_id),
                                  after={"employee_id": asn.employee_id})

    async def my_assignments(self, session: AsyncSession, employee_id: int) -> list[dict]:
        """본인에게 현재 배정된(미반납) 자산 — 수령 확인 대기 표시(F-7 본인 포털)."""
        rows = (await session.execute(
            select(AssetAssignment, ITAsset)
            .join(ITAsset, ITAsset.id == AssetAssignment.asset_id)
            .where(AssetAssignment.employee_id == employee_id,
                   AssetAssignment.returned_on.is_(None))
            .order_by(AssetAssignment.id.desc()))).all()
        return [{"assignment_id": asn.id, "asset_id": a.id, "asset_tag": a.asset_tag,
                 "name": a.name, "category": a.category, "assigned_on": asn.assigned_on,
                 "acknowledged_at": asn.acknowledged_at} for asn, a in rows]

    async def change_history(self, session: AsyncSession, *, entity: str,
                             target_id: int) -> list[dict]:
        """감사 로그 기반 변경 이력 — 만기·단가 갱신 등 추적(F-8). 최신순."""
        from erp.audit.models import AuditLog
        rows = (await session.scalars(
            select(AuditLog).where(AuditLog.entity == entity,
                                   AuditLog.target_id == str(target_id))
            .order_by(AuditLog.id.desc()).limit(200))).all()
        return [{"id": r.id, "action": r.action, "actor": r.actor, "at": r.at,
                 "before": r.before, "after": r.after} for r in rows]

    async def assignments(self, session: AsyncSession, asset_id: int) -> list[AssetAssignment]:
        return list((await session.scalars(
            select(AssetAssignment).where(AssetAssignment.asset_id == asset_id)
            .order_by(AssetAssignment.id.desc()))).all())

    async def _close_open(self, session: AsyncSession, asset_id: int, *, on: date,
                          condition: str | None = None) -> None:
        for asn in (await session.scalars(select(AssetAssignment).where(
                AssetAssignment.asset_id == asset_id,
                AssetAssignment.returned_on.is_(None)))).all():
            asn.returned_on = on
            if condition:
                asn.return_condition = condition

    async def assets_csv(self, session: AsyncSession, *, category: str | None = None,
                         status: str | None = None, department_id: int | None = None,
                         handles_phi: bool | None = None, assigned: bool | None = None,
                         q: str | None = None) -> str:
        """자산 대장 CSV(감사 제출용·F-10) — 목록 필터와 동일 스코프. 사용자명 해석 포함."""
        items = await self.list_assets(session, category=category, status=status,
                                       department_id=department_id, handles_phi=handles_phi,
                                       assigned=assigned, q=q)
        names = await self.employee_names(session, [a.assigned_employee_id for a in items])
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["자산태그", "명칭", "분류", "상태", "시리얼", "제조사", "모델",
                    "사용자", "위치", "도입일", "보증만기", "IP", "호스트명", "PHI취급",
                    "보안등급", "디스크암호화", "백신", "교정만기", "폐기일"])
        for a in items:
            nm = (names.get(a.assigned_employee_id) or {}).get("name") or ""
            w.writerow([a.asset_tag, a.name, a.category, a.status, a.serial_no or "",
                        a.manufacturer or "", a.model or "", nm, a.location or "",
                        a.acquired_on or "", a.warranty_until or "", a.ip_addr or "",
                        a.hostname or "", "Y" if a.handles_phi else "", a.security_grade or "",
                        "Y" if a.disk_encrypted else ("N" if a.disk_encrypted is False else ""),
                        "Y" if a.av_installed else ("N" if a.av_installed is False else ""),
                        a.calibration_due or "", a.disposed_at or ""])
        return buf.getvalue()

    async def counts(self, session: AsyncSession) -> dict:
        rows = (await session.execute(
            select(ITAsset.status, func.count()).group_by(ITAsset.status))).all()
        return {s: c for s, c in rows}

    async def employee_names(self, session: AsyncSession, ids: list[int]) -> dict[int, dict]:
        """직원 id → {name, no} — 자산 사용자 표기용."""
        ids = [i for i in set(ids) if i]
        if not ids:
            return {}
        rows = (await session.execute(
            select(Employee.id, Employee.name, Employee.employee_no)
            .where(Employee.id.in_(ids)))).all()
        return {i: {"name": n, "employee_no": no} for i, n, no in rows}

    async def employee_options(self, session: AsyncSession, *, q: str | None = None) -> list[dict]:
        """자산 배정용 재직자 목록(id·사번·이름·부서) — 콤보박스 검색·선택."""
        from erp.common.models import Department
        stmt = (select(Employee.id, Employee.employee_no, Employee.name, Department.name)
                .outerjoin(Department, Department.id == Employee.department_id)
                .where(Employee.resigned_on.is_(None)))
        if q:
            like = like_pattern(q)
            stmt = stmt.where(Employee.name.ilike(like, escape=chr(92))
                              | Employee.employee_no.ilike(like, escape=chr(92)))
        rows = (await session.execute(stmt.order_by(Employee.employee_no).limit(1000))).all()
        return [{"id": i, "employee_no": no, "name": nm, "department": dept}
                for i, no, nm, dept in rows]

    # ── 퇴사 회수(offboarding) (M4·G-14) ─────────────────────
    async def employee_holdings(self, session: AsyncSession, employee_id: int) -> dict:
        """직원이 현재 보유 중인 IT 자산 + SW 좌석 — 퇴사 회수·인수인계용."""
        assets = list((await session.scalars(select(ITAsset).where(
            ITAsset.assigned_employee_id == employee_id,
            ITAsset.status != "DISPOSED"))).all())
        seats = (await session.execute(
            select(ServiceAllocation, SoftwareService.name)
            .join(SoftwareService, SoftwareService.id == ServiceAllocation.service_id)
            .where(ServiceAllocation.employee_id == employee_id,
                   ServiceAllocation.released_on.is_(None)))).all()
        return {
            "assets": [{"id": a.id, "asset_tag": a.asset_tag, "name": a.name,
                        "category": a.category, "status": a.status} for a in assets],
            "seats": [{"allocation_id": al.id, "service_id": al.service_id, "service_name": nm}
                      for al, nm in seats],
        }

    async def offboarding_get(self, session: AsyncSession, employee_id: int) -> Offboarding | None:
        """직원의 최신 퇴사 회수 기록(없으면 None)."""
        return await session.scalar(select(Offboarding).where(
            Offboarding.employee_id == employee_id).order_by(Offboarding.id.desc()))

    async def offboarding_create(self, session: AsyncSession, *, actor: int, employee_id: int,
                                 reviewer_id: int | None = None, handover_to_id: int | None = None,
                                 note: str | None = None) -> Offboarding:
        """퇴사 회수 기록 생성(검토자·인수인계자·비고). 진행 중 기록이 있으면 그대로 반환(멱등)."""
        if await session.get(Employee, employee_id) is None:
            raise ValidationError("존재하지 않는 직원입니다")
        existing = await session.scalar(select(Offboarding).where(
            Offboarding.employee_id == employee_id, Offboarding.status == "IN_PROGRESS"))
        if existing is not None:
            return existing
        o = Offboarding(employee_id=employee_id, reviewer_id=reviewer_id,
                        handover_to_id=handover_to_id, note=note, created_by=actor)
        session.add(o)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="itam.offboardings", target_id=str(o.id),
                                  after={"employee_id": employee_id})
        return o

    async def offboarding_update(self, session: AsyncSession, *, actor: int, offboarding_id: int,
                                 **fields) -> Offboarding:
        o = await session.get(Offboarding, offboarding_id)
        if o is None:
            raise ValidationError("퇴사 회수 기록 없음")
        if o.status == "COMPLETED":
            raise ValidationError("완료된 회수는 수정할 수 없습니다")
        before = {k: getattr(o, k) for k in fields if hasattr(o, k)}
        for k in ("reviewer_id", "handover_to_id", "note"):
            if k in fields:
                setattr(o, k, fields[k])
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="itam.offboardings", target_id=str(o.id),
                                  before=before, after=fields)
        return o

    async def offboarding_complete(self, session: AsyncSession, *, actor: int,
                                   offboarding_id: int) -> Offboarding:
        """회수 완료 — 미회수 자산·좌석이 남아 있으면 차단(회수 누락 방지)."""
        o = await session.get(Offboarding, offboarding_id)
        if o is None:
            raise ValidationError("퇴사 회수 기록 없음")
        if o.status == "COMPLETED":
            raise ValidationError("이미 완료된 회수입니다")
        holdings = await self.employee_holdings(session, o.employee_id)
        if holdings["assets"] or holdings["seats"]:
            raise ValidationError("미회수 IT 자산·SW 좌석이 남아 있어 완료할 수 없습니다")
        o.status = "COMPLETED"
        o.completed_on = date.today()
        o.completed_by = actor
        await _audit.record_audit(session, actor=actor, action="COMPLETE",
                                  entity="itam.offboardings", target_id=str(o.id),
                                  after={"employee_id": o.employee_id})
        return o

    async def offboarding_pending(self, session: AsyncSession) -> list[dict]:
        """퇴사(resigned_on)했으나 IT 자산·SW 좌석을 아직 회수하지 못한 직원."""
        asset_ct = dict((await session.execute(
            select(ITAsset.assigned_employee_id, func.count())
            .where(ITAsset.assigned_employee_id.isnot(None), ITAsset.status != "DISPOSED")
            .group_by(ITAsset.assigned_employee_id))).all())
        seat_ct = dict((await session.execute(
            select(ServiceAllocation.employee_id, func.count())
            .where(ServiceAllocation.employee_id.isnot(None),
                   ServiceAllocation.released_on.is_(None))
            .group_by(ServiceAllocation.employee_id))).all())
        holders = {e for e in (asset_ct.keys() | seat_ct.keys()) if e is not None}
        if not holders:
            return []
        resigned = (await session.scalars(select(Employee).where(
            Employee.id.in_(holders), Employee.resigned_on.isnot(None)))).all()
        return [{"employee_id": e.id, "employee_no": e.employee_no, "name": e.name,
                 "resigned_on": str(e.resigned_on), "asset_count": asset_ct.get(e.id, 0),
                 "seat_count": seat_ct.get(e.id, 0)}
                for e in sorted(resigned, key=lambda x: x.resigned_on or x.name)]

    # ── 유지보수 이력 (M3·G-6) ────────────────────────────────
    async def add_maintenance(self, session: AsyncSession, *, actor: int, asset_id: int,
                              kind: str, on_date: date, vendor: str | None = None,
                              cost: float | None = None, note: str | None = None) -> MaintenanceRecord:
        await self.get_asset(session, asset_id)
        if kind not in MAINTENANCE_KINDS:
            raise ValidationError(f"유지보수 유형 오류: {kind}")
        m = MaintenanceRecord(it_asset_id=asset_id, kind=kind, on_date=on_date, vendor=vendor,
                              cost=cost, note=note, created_by=actor)
        session.add(m)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="MAINTENANCE",
                                  entity="itam.it_assets", target_id=str(asset_id),
                                  after={"kind": kind, "on_date": str(on_date)})
        return m

    async def maintenance_records(self, session: AsyncSession,
                                  asset_id: int) -> list[MaintenanceRecord]:
        return list((await session.scalars(
            select(MaintenanceRecord).where(MaintenanceRecord.it_asset_id == asset_id)
            .order_by(MaintenanceRecord.on_date.desc(), MaintenanceRecord.id.desc()))).all())

    # ── 폐기 + 데이터 소거 게이트 (M3·G-7/G-12) ───────────────
    async def dispose(self, session: AsyncSession, *, actor: int, asset_id: int,
                      data_erased: bool, on: date | None = None, note: str | None = None) -> ITAsset:
        """자산 폐기 — **PHI 취급(또는 과거 취급) 장비는 데이터 소거 확인 후에만 폐기 가능**(정보보호 감사·S-2).

        F-3 폐기 결재 게이트: ITAM_DISPOSAL 결재 규칙(금액 밴드)이 있으면 **승인 후에만** 폐기.
        규칙이 없으면(기본) 기존처럼 즉시 폐기(무중단 호환·PO 레거시 폴백 패턴).
        """
        a = await self.get_asset(session, asset_id)
        if a.status == "DISPOSED":
            raise ValidationError("이미 폐기된 자산입니다")
        if (a.handles_phi or a.phi_ever) and not data_erased:
            raise ValidationError("PHI 취급 장비는 데이터 소거 확인 후 폐기할 수 있습니다(소거 확인서 첨부 권장)")
        # F-3 결재 게이트 — 미승인 시 결재선 상신 후 중단(승인 후 다시 폐기)
        # 재검토 HIGH — 결재 금액은 **서버 도출**(연결 고정자산 장부가). 클라이언트 book_value로 임계 우회 차단.
        cur = await _approval.get(session, doc_type=_DISPOSAL_DOC, doc_id=asset_id)
        if cur is None or cur.get("status") != "APPROVED":
            amount = await self._disposal_amount(session, a)
            inst = await _approval.build(session, doc_type=_DISPOSAL_DOC, doc_id=asset_id,
                                         amount=amount, created_by=actor,
                                         department_id=a.owner_department_id)
            if inst is not None:      # 매칭 규칙 존재 → 승인 필요(멱등: 진행 중이면 재상신 안 함)
                raise ValidationError("이 폐기는 결재 승인이 필요합니다 — 결재선이 상신되었습니다(승인 후 다시 폐기)")
            # inst None = 매칭 규칙 없음 → 즉시 폐기(기존 동작)
        # 폐기 시 사용 중이면 반납 처리
        await self._close_open(session, asset_id, on=on or date.today())
        a.assigned_employee_id = None
        a.status = "DISPOSED"
        a.data_erased = data_erased
        a.disposed_at = on or date.today()
        if note:
            a.note = note
        await _audit.record_audit(session, actor=actor, action="DISPOSE", entity="itam.it_assets",
                                  target_id=str(asset_id),
                                  after={"data_erased": data_erased, "disposed_at": str(a.disposed_at)})
        return a

    async def _disposal_amount(self, session: AsyncSession, a: ITAsset) -> float:
        """폐기 결재 임계 기준 금액 — 연결 고정자산 장부가(취득−감가상각). 미연결=0(비자본화·저가).

        클라이언트 입력을 신뢰하지 않는다(임계 우회 방지). 자본화된 고액 자산은 finance 연결이 SoT.
        """
        if a.finance_asset_id is None:
            return 0.0
        from erp.finance.models import FixedAsset
        fa = await session.get(FixedAsset, a.finance_asset_id)
        if fa is None:
            return 0.0
        return max(0.0, float(fa.acquisition_cost) - float(fa.accumulated_depreciation))

    async def disposal_approval(self, session: AsyncSession, *, actor: int, asset_id: int,
                                approve: bool, override: bool = False,
                                note: str | None = None) -> dict:
        """폐기 결재 승인/반려(F-3) — 결재 엔진 직무분리·단계 자격 검증. 승인 완료 후 재폐기로 확정."""
        await self.get_asset(session, asset_id)
        inst = await _approval.act(session, doc_type=_DISPOSAL_DOC, doc_id=asset_id, actor=actor,
                                   approve=approve, note=note or "", override=override)
        return {"asset_id": asset_id, "status": inst.status}

    async def disposal_status(self, session: AsyncSession, asset_id: int) -> dict | None:
        """자산 폐기 결재 진행 상태(없으면 None) — 상세 화면 표시용."""
        return await _approval.get(session, doc_type=_DISPOSAL_DOC, doc_id=asset_id)
