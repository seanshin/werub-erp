"""파일 드롭 수신 어댑터 (M0a 후보 1순위 — design §5.1).

HIS·청구 모듈이 공유 디렉터리에 통보서 파일을 떨어뜨리면 스테이징에 등록.
연계 방식(M0a)이 API/DB로 확정되어도 fallback 경로로 유지 (Plan FR-38).

동작: NOTICE_DROP_DIR 스캔 → 안정화 확인(크기 변화 없음) → NoticeUploadService
재사용(§6.7 검증 동일 적용) → 원본은 processed/로 이동 (재수집 방지).
"""
import logging
import os
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from erp.claims.service import NoticeUploadService
from erp.errors import ValidationError

logger = logging.getLogger("erp.integrations.file_drop")

SYSTEM_ACTOR = 0  # 시스템 수집 표식 (audit actor)


class FileDropCollector:
    def __init__(self, drop_dir: str | None = None,
                 uploads: NoticeUploadService | None = None):
        self._dir = Path(drop_dir or os.environ.get("NOTICE_DROP_DIR", "notice-drop"))
        self._uploads = uploads or NoticeUploadService()

    async def collect(self, session: AsyncSession, *, limit: int = 50) -> dict:
        """드롭 디렉터리 수집 — staging 등록 후 processed/ 이동."""
        if not self._dir.exists():
            return {"collected": 0, "rejected": 0}
        processed = self._dir / "processed"
        rejected_dir = self._dir / "rejected"
        collected = rejected = 0

        for path in sorted(self._dir.iterdir()):
            if not path.is_file() or path.name.startswith("."):
                continue
            if collected + rejected >= limit:
                break
            try:
                content = path.read_bytes()
                await self._uploads.upload(session, actor=SYSTEM_ACTOR,
                                           original_name=path.name, content=content)
                processed.mkdir(exist_ok=True)
                path.rename(processed / path.name)
                collected += 1
            except ValidationError as exc:
                # §6.7 검증 탈락 — 격리 디렉터리로 (운영자 확인)
                rejected_dir.mkdir(exist_ok=True)
                path.rename(rejected_dir / path.name)
                rejected += 1
                logger.warning("드롭 파일 거부", extra={"ctx": {
                    "file": path.name, "error": str(exc)}})
        if collected or rejected:
            logger.info("파일 드롭 수집", extra={"ctx": {
                "collected": collected, "rejected": rejected}})
        return {"collected": collected, "rejected": rejected}
