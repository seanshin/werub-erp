"""감사·접속기록 세밀 필터 — 기간(from/to)·대상ID·count 페이지네이션."""
from datetime import datetime, timezone

from erp.app.admin_router import list_access_logs, list_audit_logs
from erp.audit.models import AccessLog, AuditLog
from erp.common.router import CurrentUser

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=None, license_flag=True)


def _at(y, m, d):
    return datetime(y, m, d, 12, 0, tzinfo=timezone.utc)


async def _seed_audit(session):
    session.add_all([
        AuditLog(actor=10, action="UPDATE", entity="finance.vouchers", target_id="100",
                 at=_at(2026, 6, 1)),
        AuditLog(actor=11, action="DELETE", entity="hr.payroll", target_id="200",
                 at=_at(2026, 6, 10)),
        AuditLog(actor=10, action="CREATE", entity="finance.vouchers", target_id="101",
                 at=_at(2026, 6, 20)),
    ])
    await session.flush()


async def test_audit_date_range_filter(session):
    await _seed_audit(session)
    out = await list_audit_logs(user=ADMIN, session=session,
                                date_from="2026-06-05", date_to="2026-06-15")
    assert out["count"] == 1 and out["items"][0]["target_id"] == "200"


async def test_audit_target_id_and_actor_filter(session):
    await _seed_audit(session)
    out = await list_audit_logs(user=ADMIN, session=session, actor=10, target_id="101")
    assert out["count"] == 1 and out["items"][0]["action"] == "CREATE"
    # 대상ID 부분일치(1XX) — actor 10의 100·101 두 건
    out2 = await list_audit_logs(user=ADMIN, session=session, actor=10, target_id="10")
    assert out2["count"] == 2


async def test_audit_date_to_inclusive(session):
    await _seed_audit(session)
    # 종료일 당일(6/20) 포함되어야 함
    out = await list_audit_logs(user=ADMIN, session=session, date_from="2026-06-20",
                                date_to="2026-06-20")
    assert out["count"] == 1 and out["items"][0]["target_id"] == "101"


async def test_access_route_filter(session):
    session.add_all([
        AccessLog(actor=5, action="EXPORT", entity="hr.employees",
                  route="/export/employees.xlsx", at=_at(2026, 6, 2)),
        AccessLog(actor=5, action="VIEW", entity="patient.patients",
                  route="/patients", at=_at(2026, 6, 3)),
    ])
    await session.flush()
    out = await list_access_logs(user=ADMIN, session=session, route="export")
    assert out["count"] == 1 and out["items"][0]["action"] == "EXPORT"


# ---- before/after 노출 + 민감값 마스킹 ----

async def test_audit_log_exposes_masked_before_after(session, client):
    """변경 전/후를 보여주되 계좌·급여·부양가족·주민번호는 서버가 가린다.

    감사로그 스냅샷은 도메인마다 다른 키를 담아 미리 알 수 없다. 그대로 내보내면
    화면에서 마스킹하던 계좌번호가 감사로그 경유로 평문 노출된다(마스킹 우회).
    """
    from erp.audit.models import AuditLog
    from tests._http import auth_as

    session.add(AuditLog(
        actor=1, action="UPDATE", entity="finance.corporate_account", target_id="6",
        before={"account_no": "110-123-456789", "alias": "구", "base_pay": 4200000},
        after={"account_no": "110-999-000111", "alias": "신",
               "dependents": [{"name": "A"}, {"name": "B"}],
               "note": "주민 900101-1234567 확인", "amount": 1500}))
    await session.flush()

    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/audit-logs", headers=admin)
    assert r.status_code == 200, r.text
    it = r.json()["items"][0]

    assert it["before"]["alias"] == "구" and it["after"]["amount"] == 1500   # 평범한 값은 보인다
    assert it["before"]["account_no"] == "***" and it["after"]["account_no"] == "***"
    assert it["before"]["base_pay"] == "***"
    assert it["after"]["dependents"] == "[2건]"          # 개수만
    assert "900101-1234567" not in r.text                # 자유서술 안에 박힌 것도 안 샌다
    assert "110-999-000111" not in r.text                # 본문 어디에도 계좌 평문 없음
