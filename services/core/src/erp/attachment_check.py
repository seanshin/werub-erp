"""첨부 본문 존재 점검 — `python -m erp.attachment_check` (배포 후 1회·정기 점검용).

v1.288 배포 전에는 서명 완료본 회수 잡(worker-batch)에 첨부 볼륨이 없어, 회수한 PDF가 그 컨테이너
레이어에만 쓰였다. 재생성되면 사라지고, 계약의 `sign_doc_attachment_id`는 남아 재회수도 일어나지 않는다
(회수 URL은 24시간 토큰이라 다시 큐에 넣어도 실패한다). 이 점검은 **메타는 있는데 본문이 없는 첨부**를
찾아 목록으로 낸다 — 복구는 배포 전 파일 복사(런북) 또는 HIS에 완료본 재발급 요청.
"""
import asyncio
import sys

from sqlalchemy import select


async def find_missing(session) -> list[dict]:
    from erp.common.attachment_service import _path
    from erp.common.models import Attachment
    from erp.config import get_settings
    base = get_settings().attachment_dir
    if not base:
        return [{"error": "ATTACHMENT_DIR 미설정"}]
    rows = (await session.execute(select(Attachment.id, Attachment.entity_type, Attachment.entity_id,
                                         Attachment.filename, Attachment.sha256)
                                  .order_by(Attachment.id))).all()

    def _scan():   # 파일 존재 확인은 스레드에서 — 이벤트 루프를 막지 않게(정합성 점검이 API에서도 돈다)
        return [{"id": r.id, "entity_type": r.entity_type, "entity_id": r.entity_id,
                 "filename": r.filename} for r in rows if not _path(base, r.sha256).exists()]
    return await asyncio.to_thread(_scan)


async def _main() -> int:
    from erp.db import session_factory
    async with session_factory()() as session:
        missing = await find_missing(session)
    for m in missing:
        print(m)
    print(f"본문 없는 첨부: {len([m for m in missing if 'id' in m])}건")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
