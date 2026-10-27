"""아웃바운드 허용 호스트 — 설정만으로 다른 기관에 설치 가능.

종전엔 운영 도메인이 모듈마다 코드에 박혀 있어 다른 호스트에서는 코드를 고쳐야 했다.
"""
import pytest

from erp.config import get_settings
from erp.integrations.egress import allowed_hosts, check_host


@pytest.fixture
def other_site(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "his_api_base", "https://his.other-hospital.kr")
    monkeypatch.setattr(s, "clinic_api_base", "https://gw.other-hospital.kr")
    monkeypatch.setattr(s, "weve_ai_api_base", "https://ai.other-hospital.kr/ollama")
    monkeypatch.setattr(s, "his_allowed_hosts", "his.other-hospital.kr")
    monkeypatch.setattr(s, "clinic_allowed_hosts", "gw.other-hospital.kr")
    monkeypatch.setattr(s, "ai_allowed_hosts", "ai.other-hospital.kr")
    return s


def test_base_alone_does_not_open_egress(monkeypatch):
    """INV-NET-01 — base URL만 바꿔서는 나가지 않는다(오설정이 곧 유출이 되지 않게)."""
    monkeypatch.setattr(get_settings(), "his_api_base", "https://his.other-hospital.kr")
    assert "his.other-hospital.kr" not in allowed_hosts("his")


def test_configured_hosts_are_allowed(other_site):
    assert "his.other-hospital.kr" in allowed_hosts("his")
    assert "gw.other-hospital.kr" in allowed_hosts("clinic")
    assert "ai.other-hospital.kr" in allowed_hosts("ai")
    assert "his.example.org" in allowed_hosts("his")          # 기존 기본값 유지(무회귀)


def test_extra_csv_and_loopback(monkeypatch, other_site):
    monkeypatch.setattr(other_site, "his_allowed_hosts", " Relay.Local , his2.other.kr")
    hosts = allowed_hosts("his")
    assert {"relay.local", "his2.other.kr"} <= hosts and "localhost" not in hosts
    assert "localhost" in allowed_hosts("his", loopback=True)


def test_unlisted_host_rejected_with_setting_name(other_site):
    with pytest.raises(ValueError, match="HIS_ALLOWED_HOSTS"):
        check_host("his", "https://evil.example.com/x")


def test_clients_follow_configuration(other_site):
    """코드 고정 목록이던 클라이언트들이 설정 호스트로 생성된다."""
    from erp.integrations.ai_publisher import HttpAIIndexPublisher
    from erp.integrations.clinic_staff import RestClinicStaffSource
    from erp.integrations.eapproval import EApprovalClient
    from erp.integrations.groupware import RestGroupwareAttendanceSource
    from erp.integrations.his_fee_push import HISFeePushClient
    from erp.integrations.weve_rag import WeveRagClient
    HISFeePushClient("https://his.other-hospital.kr", "k")
    EApprovalClient("https://his.other-hospital.kr", "k", "H")
    RestGroupwareAttendanceSource("https://gw.other-hospital.kr", "k", "H")
    RestClinicStaffSource("https://gw.other-hospital.kr", "k", "H")
    WeveRagClient("https://ai.other-hospital.kr/ollama", "k")
    HttpAIIndexPublisher("https://ai.other-hospital.kr/ollama/api/notice", "k")
    with pytest.raises(ValueError):
        HISFeePushClient("https://evil.example.com", "k")


async def test_signed_doc_fetch_still_rejects_payload_hosts(other_site):
    """본문 유래 URL(서명 완료본)은 설정 호스트만 — 임의 호스트는 여전히 거부."""
    from erp.errors import ValidationError
    from erp.integrations.esign_fetch import fetch_signed_doc
    with pytest.raises(ValidationError, match="허용되지 않은"):
        await fetch_signed_doc(None, entity_type="x", entity_id=1,
                               url="https://evil.example.com/doc.pdf")


async def test_status_endpoint_shows_effective_egress(session, client, other_site):
    from tests._http import auth_as
    admin = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/integrations/status", headers=admin)
    assert r.status_code == 200, r.text
    assert "his.other-hospital.kr" in r.json()["egress"]["his"]


def test_attachment_dirs_become_group_writable(tmp_path):
    """첨부 공유 볼륨 — 기존 755 샤드를 core 기동 시 2775로 정비(worker-batch가 쓸 수 있게).

    실제 컨테이너 검증(2026-10-08·colima): 정비 전 uid 10003(group 10001)이 기존 샤드에
    PermissionError → 정비 후 양방향 쓰기·읽기·삭제 성공.
    """
    import os

    from erp.common.attachment_service import ensure_shared_permissions
    shard = tmp_path / "ab"
    shard.mkdir()
    os.chmod(tmp_path, 0o755)
    os.chmod(shard, 0o755)
    assert ensure_shared_permissions(str(tmp_path)) == 2
    assert (shard.stat().st_mode & 0o7777) == 0o2775
    assert ensure_shared_permissions(str(tmp_path)) == 0        # 멱등
    assert ensure_shared_permissions(None) == 0


async def test_attachment_check_finds_missing_bodies(session, tmp_path, monkeypatch):
    """B2 — 메타는 있는데 본문이 없는 첨부를 찾는다(worker-batch 레이어 소실분 점검)."""
    from erp.attachment_check import find_missing
    from erp.common.attachment_service import AttachmentService
    monkeypatch.setattr(get_settings(), "attachment_dir", str(tmp_path))
    a = await AttachmentService().save(session, entity_type="contract", entity_id=1,
                                       filename="x.pdf", content_type="application/pdf",
                                       data=b"%PDF-1.4 test", uploaded_by=None)
    assert await find_missing(session) == []
    from erp.common.attachment_service import _path
    _path(str(tmp_path), a.sha256).unlink()
    assert [m["id"] for m in await find_missing(session)] == [a.id]
