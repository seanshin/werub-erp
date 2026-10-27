"""ITAM M2 — 소프트웨어·디지털 서비스 마스터·좌석 배정·라이선스 준수."""
from datetime import date

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.audit.service import AuditService
from erp.common import crypto
from erp.errors import ValidationError
from erp.itam.models import (
    BILLING_MODELS, PAYMENT_TERMS, SERVICE_TYPES, ServiceAllocation, SoftwareService,
)

_audit = AuditService()
_KEY_AAD = "itam.software_services.license_key"
_TOKEN_AAD = "itam.software_services.api_token"


def _mask_key(key: str | None) -> str | None:
    if not key:
        return None
    tail = key[-4:] if len(key) >= 4 else key
    return f"****{tail}"


class SoftwareServiceService:
    async def create(self, session: AsyncSession, *, actor: int, name: str,
                     service_type: str = "SAAS", billing_model: str = "SEAT_RECURRING",
                     license_key: str | None = None, api_token: str | None = None,
                     **fields) -> SoftwareService:
        if not name.strip():
            raise ValidationError("서비스명은 필수입니다")
        if service_type not in SERVICE_TYPES:
            raise ValidationError(f"서비스 유형 오류: {service_type}")
        if billing_model not in BILLING_MODELS:
            raise ValidationError(f"과금 유형 오류: {billing_model}")
        if fields.get("payment_terms") and fields["payment_terms"] not in PAYMENT_TERMS:
            raise ValidationError(f"결제방식 오류: {fields['payment_terms']}")
        fields.setdefault("registered_on", date.today())     # 등록일 기본=오늘
        s = SoftwareService(name=name.strip(), service_type=service_type,
                            billing_model=billing_model, created_by=actor,
                            **{k: v for k, v in fields.items()
                               if k not in ("license_key", "api_token")})
        session.add(s)
        await session.flush()   # PK 확정(암호화 AAD)
        if license_key:
            s.license_key_enc = crypto.encrypt_column(
                license_key, aad=crypto.column_aad(_KEY_AAD, s.id))
        if api_token:           # 토큰=자격증명 → AES-GCM 암호화 저장(평문 금지)
            s.api_token_enc = crypto.encrypt_column(
                api_token, aad=crypto.column_aad(_TOKEN_AAD, s.id))
        await _audit.record_audit(session, actor=actor, action="CREATE",
                                  entity="itam.software_services", target_id=str(s.id),
                                  after={"name": name, "service_type": service_type})
        return s

    async def get(self, session: AsyncSession, service_id: int) -> SoftwareService:
        s = await session.get(SoftwareService, service_id)
        if s is None:
            raise ValidationError("존재하지 않는 서비스입니다")
        return s

    async def list_services(self, session: AsyncSession, *, service_type: str | None = None,
                            status: str | None = None, q: str | None = None) -> list[SoftwareService]:
        stmt = select(SoftwareService)
        if service_type:
            stmt = stmt.where(SoftwareService.service_type == service_type)
        if status:
            stmt = stmt.where(SoftwareService.status == status)
        if q:
            from erp.itam.service import like_pattern
            like = like_pattern(q)
            stmt = stmt.where(or_(SoftwareService.name.ilike(like, escape=chr(92)),
                                  SoftwareService.vendor.ilike(like, escape=chr(92))))
        return list((await session.scalars(stmt.order_by(SoftwareService.id.desc()))).all())

    async def update(self, session: AsyncSession, *, actor: int, service_id: int,
                     license_key: str | None = None, api_token: str | None = None,
                     **fields) -> SoftwareService:
        s = await self.get(session, service_id)
        if "service_type" in fields and fields["service_type"] not in SERVICE_TYPES:
            raise ValidationError("서비스 유형 오류")
        if "billing_model" in fields and fields["billing_model"] not in BILLING_MODELS:
            raise ValidationError("과금 유형 오류")
        # F-8 — SW update도 변경 필드 이전값 대칭 수집(ITAsset와 동형)
        before = {k: getattr(s, k) for k in fields if hasattr(s, k)}
        for k, v in fields.items():
            if hasattr(s, k) and k not in ("id", "created_by", "created_at", "license_key_enc",
                                           "api_token_enc", "status", "cancelled_on"):
                setattr(s, k, v)
        after = dict(fields)
        if license_key:      # S-11 — 키 변경은 값 제외 이벤트로 감사(흔적 보존)
            s.license_key_enc = crypto.encrypt_column(
                license_key, aad=crypto.column_aad(_KEY_AAD, s.id))
            after["license_key"] = "KEY_SET"
        if api_token:        # 토큰 변경도 값 제외 감사
            s.api_token_enc = crypto.encrypt_column(
                api_token, aad=crypto.column_aad(_TOKEN_AAD, s.id))
            after["api_token"] = "TOKEN_SET"
        await _audit.record_audit(session, actor=actor, action="UPDATE",
                                  entity="itam.software_services", target_id=str(s.id),
                                  before=before, after=after)
        return s

    def _masked(self, enc: bytes | None, aad_key: str, sid: int) -> str | None:
        if enc is None:
            return None
        try:                                  # S-10 — 복호 실패 1건이 목록 EP 전체를 500내지 않도록 격리
            return _mask_key(crypto.decrypt_column(enc, aad=crypto.column_aad(aad_key, sid)))
        except Exception:                     # noqa: BLE001
            return "복호화 실패"

    def masked_key(self, s: SoftwareService) -> str | None:
        return self._masked(s.license_key_enc, _KEY_AAD, s.id)

    def masked_token(self, s: SoftwareService) -> str | None:
        return self._masked(s.api_token_enc, _TOKEN_AAD, s.id)

    async def cancel(self, session: AsyncSession, *, actor: int, service_id: int,
                     reactivate: bool = False) -> SoftwareService:
        """서비스 해지/재개(F-2) — 해지 시 점검·준수·대시보드 대상에서 제외."""
        s = await self.get(session, service_id)
        s.status = "ACTIVE" if reactivate else "CANCELLED"
        s.cancelled_on = None if reactivate else date.today()
        await _audit.record_audit(session, actor=actor,
                                  action="REACTIVATE" if reactivate else "CANCEL",
                                  entity="itam.software_services", target_id=str(service_id))
        return s

    # ── 좌석 배정·준수 ────────────────────────────────────────
    async def allocate(self, session: AsyncSession, *, actor: int, service_id: int,
                       employee_id: int | None = None, it_asset_id: int | None = None,
                       note: str | None = None) -> ServiceAllocation:
        await self.get(session, service_id)
        if employee_id is None and it_asset_id is None:
            raise ValidationError("직원 또는 자산 중 하나를 지정하세요")
        if employee_id is not None:
            from erp.hr.models import Employee
            if await session.get(Employee, employee_id) is None:
                raise ValidationError("존재하지 않는 직원입니다")
        # 동일 대상 중복 open 배정 방지 — used 부풀림·위반 오탐 차단(검토)
        dup = await session.scalar(select(ServiceAllocation.id).where(
            ServiceAllocation.service_id == service_id,
            ServiceAllocation.released_on.is_(None),
            (ServiceAllocation.employee_id == employee_id) if employee_id is not None
            else (ServiceAllocation.it_asset_id == it_asset_id)))
        if dup is not None:
            raise ValidationError("이미 좌석이 배정된 대상입니다")
        a = ServiceAllocation(service_id=service_id, employee_id=employee_id,
                              it_asset_id=it_asset_id, allocated_on=date.today(), note=note)
        session.add(a)
        await session.flush()
        await _audit.record_audit(session, actor=actor, action="ALLOCATE",
                                  entity="itam.software_services", target_id=str(service_id),
                                  after={"employee_id": employee_id, "it_asset_id": it_asset_id})
        return a

    async def release(self, session: AsyncSession, *, actor: int, allocation_id: int) -> None:
        a = await session.get(ServiceAllocation, allocation_id)
        if a is None:
            raise ValidationError("배정 기록 없음")
        if a.released_on is not None:
            raise ValidationError("이미 해제된 배정입니다")
        a.released_on = date.today()
        await _audit.record_audit(session, actor=actor, action="RELEASE",
                                  entity="itam.software_services", target_id=str(a.service_id),
                                  after={"allocation_id": allocation_id, "employee_id": a.employee_id})

    async def allocations(self, session: AsyncSession, service_id: int,
                          *, open_only: bool = False) -> list[ServiceAllocation]:
        stmt = select(ServiceAllocation).where(ServiceAllocation.service_id == service_id)
        if open_only:
            stmt = stmt.where(ServiceAllocation.released_on.is_(None))
        return list((await session.scalars(stmt.order_by(ServiceAllocation.id.desc()))).all())

    async def compliance(self, session: AsyncSession, service_id: int) -> dict:
        """좌석 준수 — open 배정 수 vs 보유 좌석. 초과=위반·미사용=절감 대상."""
        s = await self.get(session, service_id)
        used = await session.scalar(select(func.count()).where(
            ServiceAllocation.service_id == service_id,
            ServiceAllocation.released_on.is_(None))) or 0
        seats = s.seats
        return {"seats": seats, "used": used,
                "over": bool(seats is not None and used > seats),
                "unused": (max(0, seats - used) if seats is not None else None),
                # 좌석 보유 여부로 판정 — 구매 SW(ONE_TIME)도 좌석 라이선스 준수 대상(검토 M)
                "seat_based": seats is not None}
