"""증빙 문서 첨부·보관 — 저장(sha256 중복제거)·보안(타입·용량·미설정)·삭제."""
import pytest

from erp.common.attachment_service import AttachmentService
from erp.config import get_settings
from erp.errors import ValidationError

svc = AttachmentService()
PDF = b"%PDF-1.4 dummy content"


def _enable(monkeypatch, tmp_path, max_bytes=20 * 1024 * 1024):
    s = get_settings()
    monkeypatch.setattr(s, "attachment_dir", str(tmp_path), raising=False)
    monkeypatch.setattr(s, "attachment_max_bytes", max_bytes, raising=False)


async def test_save_list_read_and_dedup(session, tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    a1 = await svc.save(session, entity_type="tax_invoice", entity_id=1, filename="세금계산서.pdf",
                        content_type="application/pdf", data=PDF, uploaded_by=9)
    assert a1.size_bytes == len(PDF) and len(a1.sha256) == 64
    # 동일 내용 재첨부 → 같은 sha(본문 1회 저장), 행은 2건
    a2 = await svc.save(session, entity_type="tax_invoice", entity_id=1, filename="복사본.pdf",
                        content_type="application/pdf", data=PDF, uploaded_by=9)
    assert a2.sha256 == a1.sha256
    rows = await svc.list_for(session, entity_type="tax_invoice", entity_id=1)
    assert len(rows) == 2
    assert svc.read_bytes(a1) == PDF                       # 본문 복원
    files = list(tmp_path.rglob("*"))
    assert sum(1 for f in files if f.is_file()) == 1       # 중복제거 — 본문 1개


async def test_disallowed_type_and_oversize_and_empty(session, tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path, max_bytes=10)
    with pytest.raises(ValidationError, match="형식"):
        await svc.save(session, entity_type="voucher", entity_id=1, filename="x.exe",
                       content_type="application/x-msdownload", data=PDF, uploaded_by=1)
    with pytest.raises(ValidationError, match="용량"):
        await svc.save(session, entity_type="voucher", entity_id=1, filename="big.pdf",
                       content_type="application/pdf", data=b"0" * 50, uploaded_by=1)
    with pytest.raises(ValidationError, match="빈 파일"):
        await svc.save(session, entity_type="voucher", entity_id=1, filename="e.pdf",
                       content_type="application/pdf", data=b"", uploaded_by=1)


async def test_disabled_when_dir_unset(session, monkeypatch):
    monkeypatch.setattr(get_settings(), "attachment_dir", None, raising=False)
    with pytest.raises(ValidationError, match="미설정"):
        await svc.save(session, entity_type="voucher", entity_id=1, filename="a.pdf",
                       content_type="application/pdf", data=PDF, uploaded_by=1)


async def test_delete_removes_body_when_unreferenced(session, tmp_path, monkeypatch):
    _enable(monkeypatch, tmp_path)
    a = await svc.save(session, entity_type="voucher", entity_id=5, filename="r.pdf",
                       content_type="application/pdf", data=b"%PDF unique-1", uploaded_by=1)
    await svc.delete(session, att=a)
    assert await svc.list_for(session, entity_type="voucher", entity_id=5) == []
    assert sum(1 for f in tmp_path.rglob("*") if f.is_file()) == 0   # 본문도 제거


async def test_employee_attachment_row_idor_blocked(session, tmp_path, monkeypatch):
    """S1: 직원 서류 첨부는 본인(SELF) 것만 — id 열거로 타인 서류 열람 차단."""
    from datetime import date

    from erp.common.attachment_router import download_attachment, list_attachments
    from erp.common.models import User
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    from erp.hr.models import Employee
    _enable(monkeypatch, tmp_path)
    me = Employee(employee_no="A-ME", name="본인", hired_on=date(2025, 1, 1),
                  job_group="NURSE", employment_type="REGULAR", source="ERP")
    other = Employee(employee_no="A-OT", name="타인", hired_on=date(2025, 1, 1),
                     job_group="NURSE", employment_type="REGULAR", source="ERP")
    session.add_all([me, other])
    await session.flush()
    u = User(email="att-me@x.example", password_hash="x", role="STAFF", employee_id=me.id)
    session.add(u)
    await session.flush()
    staff = CurrentUser(user_id=u.id, role="STAFF", dept_id=None, license_flag=True)
    mine = await svc.save(session, entity_type="employee", entity_id=me.id, filename="m.pdf",
                          content_type="application/pdf", data=b"%PDF mine", uploaded_by=u.id)
    theirs = await svc.save(session, entity_type="employee", entity_id=other.id, filename="o.pdf",
                            content_type="application/pdf", data=b"%PDF theirs", uploaded_by=1)

    await list_attachments(entity_type="employee", entity_id=me.id, user=staff, session=session)
    await download_attachment(attachment_id=mine.id, user=staff, session=session)   # 본인 OK
    with pytest.raises(ForbiddenError):       # 타인 직원 목록 차단
        await list_attachments(entity_type="employee", entity_id=other.id,
                               user=staff, session=session)
    with pytest.raises(ForbiddenError):       # 타인 첨부 id 직접 다운로드 차단
        await download_attachment(attachment_id=theirs.id, user=staff, session=session)


async def test_contract_attachment_self_only(session, tmp_path, monkeypatch):
    """S1: 의료진 계약 서류(보수 민감)는 본인 계약만 — STAFF SELF 행 검증."""
    from datetime import date

    from erp.common.attachment_router import download_attachment
    from erp.common.models import User
    from erp.common.router import CurrentUser
    from erp.errors import ForbiddenError
    from erp.hr.models import Employee, MedicalStaffContract
    _enable(monkeypatch, tmp_path)
    me = Employee(employee_no="C-ME", name="의사본인", hired_on=date(2025, 1, 1),
                  job_group="DOCTOR", employment_type="REGULAR", source="ERP")
    other = Employee(employee_no="C-OT", name="의사타인", hired_on=date(2025, 1, 1),
                     job_group="DOCTOR", employment_type="REGULAR", source="ERP")
    session.add_all([me, other])
    await session.flush()
    c_other = MedicalStaffContract(employee_id=other.id, valid_from=date(2026, 1, 1),
                                   base_pay=10_000_000)
    session.add(c_other)
    await session.flush()
    u = User(email="c-me@x.example", password_hash="x", role="STAFF", employee_id=me.id)
    session.add(u)
    await session.flush()
    staff = CurrentUser(user_id=u.id, role="STAFF", dept_id=None, license_flag=True)
    att = await svc.save(session, entity_type="contract", entity_id=c_other.id, filename="c.pdf",
                         content_type="application/pdf", data=b"%PDF contract", uploaded_by=1)
    with pytest.raises(ForbiddenError):       # 타 의료진 계약서 차단
        await download_attachment(attachment_id=att.id, user=staff, session=session)


async def test_magic_byte_mismatch_rejected(session, tmp_path, monkeypatch):
    """선언 형식(image/png)과 실제 내용 불일치 차단(재검토 LOW)."""
    _enable(monkeypatch, tmp_path)
    with pytest.raises(ValidationError, match="일치하지 않"):
        await svc.save(session, entity_type="it_asset", entity_id=1, filename="fake.png",
                       content_type="image/png", data=b"not-a-png-at-all", uploaded_by=1)
    # 진짜 PNG 시그니처는 통과
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 20
    a = await svc.save(session, entity_type="it_asset", entity_id=1, filename="ok.png",
                       content_type="image/png", data=png, uploaded_by=1)
    assert a.content_type == "image/png"
