"""감사·접속기록 — 동기 폴백·변경 감사 (design §3.5)."""
from sqlalchemy import select

from erp.audit.models import AccessLog, AuditLog
from erp.audit.service import AuditService


async def test_access_log_sync_fallback_without_redis(session):
    svc = AuditService(redis=None)  # Redis 없음 → 동기 INSERT 폴백
    await svc.record_access(session, actor=7, action="VIEW",
                            entity="patient.patients", target_id="123", route="/patients/123")
    await session.flush()
    row = (await session.execute(select(AccessLog))).scalar_one()
    assert row.actor == 7 and row.action == "VIEW" and row.entity == "patient.patients"


async def test_audit_log_records_before_after(session):
    svc = AuditService()
    await svc.record_audit(session, actor=1, action="UPDATE", entity="common.departments",
                           target_id="3", before={"name": "구"}, after={"name": "신"})
    await session.flush()
    row = (await session.execute(select(AuditLog))).scalar_one()
    assert row.before == {"name": "구"} and row.after == {"name": "신"}
