"""파일 드롭 수신 어댑터 (M0a fallback) — 수집·격리·§6.7 검증 재사용."""
from sqlalchemy import select

from erp.claims.models import NoticeFileStaging
from erp.claims.service import NoticeUploadService
from erp.integrations.file_drop import FileDropCollector


async def test_collect_valid_file(session, tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "notice1.txt").write_bytes(b"UNH+M1+MEDRLT'RFF+ACE:123'UNT+2+M1'")
    collector = FileDropCollector(
        drop_dir=str(drop), uploads=NoticeUploadService(storage_dir=str(tmp_path / "store")))

    result = await collector.collect(session)
    assert result == {"collected": 1, "rejected": 0}
    staging = (await session.execute(select(NoticeFileStaging))).scalar_one()
    assert staging.original_name == "notice1.txt"
    assert (drop / "processed" / "notice1.txt").exists()  # 재수집 방지 이동


async def test_collect_rejects_invalid_magic(session, tmp_path):
    """§6.7 매직바이트 검증 탈락 → rejected/ 격리 (staging 미등록)."""
    drop = tmp_path / "drop"
    drop.mkdir()
    (drop / "evil.bin").write_bytes(b"\x00\x01\x02\x03 binary junk")
    collector = FileDropCollector(
        drop_dir=str(drop), uploads=NoticeUploadService(storage_dir=str(tmp_path / "store")))

    result = await collector.collect(session)
    assert result == {"collected": 0, "rejected": 1}
    assert (drop / "rejected" / "evil.bin").exists()
    assert (await session.execute(select(NoticeFileStaging))).scalar_one_or_none() is None


async def test_collect_missing_dir_noop(session):
    collector = FileDropCollector(drop_dir="/nonexistent-drop-dir")
    assert await collector.collect(session) == {"collected": 0, "rejected": 0}
