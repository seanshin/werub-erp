"""증빙 문서 첨부 저장·조회 (공통) — 디스크 sha256 키 저장(중복제거)·보안(타입·용량·경로).

본문은 attachment_dir 하위에 내용 해시(sha256) 키로 저장 → 동일 파일 중복 제거.
DB(Attachment)는 메타데이터만. 경로는 해시로 구성(사용자 파일명 미사용 — LFI/traversal 차단).
"""
import hashlib
import os
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from erp.common.models import Attachment
from erp.config import get_settings
from erp.errors import ValidationError

# design §6.7 — 허용 형식 화이트리스트(증빙: PDF·이미지·xlsx·csv)
ALLOWED_TYPES = frozenset({
    "application/pdf", "image/png", "image/jpeg", "image/webp",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/csv",
})
# 매직바이트 서명 — 선언 content_type 위조 방지(재검토 LOW). text/csv는 서명 없음(검사 제외).
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/webp": (b"RIFF",),   # RIFF....WEBP — 추가로 8~12바이트 WEBP 확인
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (b"PK\x03\x04",),
}


def _magic_ok(content_type: str, data: bytes) -> bool:
    sigs = _MAGIC.get(content_type)
    if sigs is None:
        return True   # csv 등 서명 없는 형식은 통과
    if not any(data.startswith(sig) for sig in sigs):
        return False
    if content_type == "image/webp":
        return data[8:12] == b"WEBP"
    return True


def _path(base: str, sha: str) -> Path:
    return Path(base) / sha[:2] / sha


# 첨부 볼륨은 core(uid 10001)와 worker-batch(uid 10003·서명 완료본 회수 잡)가 **함께 쓴다**.
# 소유자가 다른 두 프로세스가 같은 샤드 디렉터리에 쓰려면 그룹(10001) 쓰기+setgid가 필요하다
# (compose: worker-batch group_add 10001). 종전엔 worker-batch에 볼륨이 없어 회수한 PDF가 그
# 컨테이너 파일시스템에만 남았다 — core에서 안 보이고 재생성 때 사라졌다.
_DIR_MODE = 0o2775
_FILE_MODE = 0o664


def _chmod_quiet(p: Path, mode: int) -> None:
    try:
        os.chmod(p, mode)
    except OSError:          # 소유자가 아니면 못 바꾼다 — 소유자(core 기동 시 정비)가 맡는다
        pass


def ensure_shared_permissions(base: str | None) -> int:
    """첨부 루트·샤드 디렉터리를 그룹 쓰기(2775)로 정비 — core 기동 시 1회(소유 디렉터리만 성공).

    이미 운영 중인 볼륨은 755로 만들어져 있어 worker-batch가 기존 샤드에 쓰지 못한다. 반환=정비 수.
    """
    if not base:
        return 0
    root = Path(base)
    if not root.is_dir():
        return 0
    n = 0
    for d in (root, *(c for c in root.iterdir() if c.is_dir())):
        if (d.stat().st_mode & 0o7777) != _DIR_MODE:
            _chmod_quiet(d, _DIR_MODE)
            n += 1
    return n


class AttachmentService:
    async def save(self, session: AsyncSession, *, entity_type: str, entity_id: int,
                   filename: str, content_type: str, data: bytes,
                   uploaded_by: int | None) -> Attachment:
        s = get_settings()
        if not s.attachment_dir:
            raise ValidationError("첨부 저장소 미설정 (attachment_dir) — 첨부 비활성")
        if content_type not in ALLOWED_TYPES:
            raise ValidationError(f"허용되지 않은 파일 형식: {content_type}")
        if not data:
            raise ValidationError("빈 파일은 첨부할 수 없습니다")
        if len(data) > s.attachment_max_bytes:
            raise ValidationError(
                f"파일 용량 초과(최대 {s.attachment_max_bytes // (1024 * 1024)}MB)")
        if not _magic_ok(content_type, data):   # 선언 형식과 실제 내용 불일치 차단(위조 방지)
            raise ValidationError("파일 내용이 선언한 형식과 일치하지 않습니다")
        sha = hashlib.sha256(data).hexdigest()
        path = _path(s.attachment_dir, sha)
        if not path.exists():                       # 동일 내용 재업로드는 본문 재기록 안 함
            path.parent.mkdir(parents=True, exist_ok=True)
            _chmod_quiet(path.parent, _DIR_MODE)    # 공유 볼륨 — 다른 컨테이너도 이 샤드에 쓴다
            path.write_bytes(data)
            _chmod_quiet(path, _FILE_MODE)
        att = Attachment(entity_type=entity_type, entity_id=entity_id,
                         filename=(filename or "file")[:255], content_type=content_type,
                         size_bytes=len(data), sha256=sha, uploaded_by=uploaded_by)
        session.add(att)
        await session.flush()
        return att

    async def list_for(self, session: AsyncSession, *, entity_type: str,
                       entity_id: int) -> list[Attachment]:
        return list((await session.execute(
            select(Attachment).where(Attachment.entity_type == entity_type,
                                     Attachment.entity_id == entity_id)
            .order_by(Attachment.id.desc()))).scalars())

    async def get(self, session: AsyncSession, *, attachment_id: int) -> Attachment | None:
        return await session.get(Attachment, attachment_id)

    def read_bytes(self, att: Attachment) -> bytes:
        path = _path(get_settings().attachment_dir or "", att.sha256)
        if not path.exists():
            raise ValidationError("저장된 파일을 찾을 수 없습니다")
        return path.read_bytes()

    async def delete(self, session: AsyncSession, *, att: Attachment) -> None:
        sha = att.sha256
        await session.delete(att)
        await session.flush()
        # 동일 내용 참조가 더 없으면 본문도 제거(중복제거 안전)
        remaining = await session.scalar(select(func.count()).select_from(Attachment)
                                         .where(Attachment.sha256 == sha))
        if not remaining and get_settings().attachment_dir:
            _path(get_settings().attachment_dir, sha).unlink(missing_ok=True)
