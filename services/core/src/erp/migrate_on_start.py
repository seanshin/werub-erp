"""기동 시 마이그레이션 — `python -m erp.migrate_on_start` (core 컨테이너 CMD).

종전 CMD는 `alembic upgrade head && uvicorn`이라, DB가 이 이미지가 모르는 **더 새로운** 리비전에 있으면
(신버전 배포 후 구 이미지로 롤백) "Can't locate revision"으로 실패하고 `&&` 때문에 서버가 아예 뜨지 않았다.
그러면 데이터를 지우는 downgrade가 강제됐다(2026-10-09 전체 흐름 검토 H).

규칙
- DB 리비전이 이 이미지의 리비전 목록에 **있으면** upgrade head(평소 경로).
- **없으면**(더 새 DB) upgrade를 건너뛰고 경고만 — 스키마는 앞서 있고 코드만 롤백된 상태로 기동한다.
  새 컬럼·테이블은 구 코드가 쓰지 않으므로 대개 안전하다. 위험한 경우는 런북에서 다룬다.
- DB가 비어 있으면(최초 설치) upgrade head.
"""
import sys


def _db_revision(url: str) -> str | None:
    from sqlalchemy import create_engine, text
    eng = create_engine(url.replace("+asyncpg", ""))
    try:
        with eng.connect() as c:
            try:
                return c.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:  # noqa: BLE001 — 테이블 없음=최초 설치
                return None
    finally:
        eng.dispose()


def main() -> int:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from erp.config import get_settings
    cfg = Config("alembic.ini")
    known = {rev.revision for rev in ScriptDirectory.from_config(cfg).walk_revisions()}
    current = _db_revision(get_settings().database_url)
    if current is not None and current not in known:
        print(f"[migrate_on_start] DB 리비전 {current}가 이 이미지에 없음(더 새 DB) — upgrade 건너뜀"
              "(코드 롤백 상태로 기동). 런북 rollback-deploy 참조.", flush=True)
        return 0
    command.upgrade(cfg, "head")
    return 0


if __name__ == "__main__":
    sys.exit(main())
