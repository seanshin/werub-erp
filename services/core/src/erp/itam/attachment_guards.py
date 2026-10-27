"""ITAM 첨부 무결성 — 대상 존재 검증 + 폐기 자산 소거확인서 삭제 차단(검토 S-9).

common.attachment_router는 모듈경계상 도메인을 import할 수 없으므로 itam가 자신의
검증을 등록한다(의존성 역전). itam 패키지 import 시 register()가 실행된다.
"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.errors import ValidationError


async def _exists(session: AsyncSession, model, entity_id: int) -> bool:
    return await session.scalar(select(model.id).where(model.id == entity_id)) is not None


async def _asset_exists(session, entity_id):
    from erp.itam.models import ITAsset
    return await _exists(session, ITAsset, entity_id)


async def _service_exists(session, entity_id):
    from erp.itam.models import SoftwareService
    return await _exists(session, SoftwareService, entity_id)


async def _contract_exists(session, entity_id):
    from erp.itam.models import VendorContract
    return await _exists(session, VendorContract, entity_id)


async def _maintenance_exists(session, entity_id):
    from erp.itam.models import MaintenanceRecord
    return await _exists(session, MaintenanceRecord, entity_id)


async def _offboarding_exists(session, entity_id):
    from erp.itam.models import Offboarding
    return await _exists(session, Offboarding, entity_id)


async def _guard_disposed_asset(session: AsyncSession, entity_id: int) -> None:
    """폐기된 자산의 첨부(소거 확인서 등)는 삭제·교체 불가 — 정보보호 감사 증적 보존."""
    from erp.itam.models import ITAsset
    status = await session.scalar(select(ITAsset.status).where(ITAsset.id == entity_id))
    if status == "DISPOSED":
        raise ValidationError("폐기된 자산의 첨부(소거 확인서 등)는 삭제할 수 없습니다(감사 증적 보존)")


async def _guard_disposed_maintenance(session: AsyncSession, entity_id: int) -> None:
    """폐기 자산의 유지보수 증빙(소거 확인서 등) 삭제 차단(오분류 경로도 보호·재검토)."""
    from erp.itam.models import ITAsset, MaintenanceRecord
    aid = await session.scalar(select(MaintenanceRecord.it_asset_id).where(
        MaintenanceRecord.id == entity_id))
    if aid is None:
        return
    if await session.scalar(select(ITAsset.status).where(ITAsset.id == aid)) == "DISPOSED":
        raise ValidationError("폐기된 자산의 유지보수 증빙은 삭제할 수 없습니다(감사 증적 보존)")


def register() -> None:
    from erp.common.attachment_router import register_delete_guard, register_existence_check
    register_existence_check("it_asset", _asset_exists)
    register_existence_check("software_service", _service_exists)
    register_existence_check("itam_contract", _contract_exists)
    register_existence_check("itam_maintenance", _maintenance_exists)
    register_existence_check("itam_offboarding", _offboarding_exists)
    register_delete_guard("it_asset", _guard_disposed_asset)
    register_delete_guard("itam_maintenance", _guard_disposed_maintenance)
