"""연동 관리 상태 (시스템) — 설정 여부·인박스 요약. 시크릿 값 비노출 보장."""
from erp.app.admin_router import integration_status
from erp.common.router import CurrentUser

ADMIN = CurrentUser(user_id=1, role="ADMIN", dept_id=1, license_flag=True)


async def test_integration_status_structure(session):
    out = await integration_status(user=ADMIN, session=session)
    names = {i["name"] for i in out["integrations"]}
    assert {"HIS SSO", "전자결재 상신", "웹훅 게이트웨이"} <= names
    assert "inbox" in out and isinstance(out["eapproval_linked_leaves"], int)


async def test_integration_status_no_secret_values(session):
    """detail에 전체 시크릿이 노출되지 않아야(설정 시 8자+길이만)."""
    out = await integration_status(user=ADMIN, session=session)
    for i in out["integrations"]:
        d = i.get("detail")
        if d and "len" in str(d):
            assert "…" in d        # 프리픽스 8자 + 말줄임 — 전체값 아님
