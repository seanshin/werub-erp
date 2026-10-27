"""기동 마이그레이션 — DB가 이미지보다 새 리비전이면 upgrade를 건너뛰고 기동(롤백 가능성·검토 H)."""
from erp import migrate_on_start


def test_skips_when_db_ahead(monkeypatch):
    calls = []
    monkeypatch.setattr(migrate_on_start, "_db_revision", lambda url: "9999")
    import alembic.command
    monkeypatch.setattr(alembic.command, "upgrade", lambda cfg, rev: calls.append(rev))
    assert migrate_on_start.main() == 0 and calls == []


def test_upgrades_when_known_or_empty(monkeypatch):
    import alembic.command
    calls = []
    monkeypatch.setattr(alembic.command, "upgrade", lambda cfg, rev: calls.append(rev))
    for rev in (None, "0247"):
        monkeypatch.setattr(migrate_on_start, "_db_revision", lambda url, r=rev: r)
        migrate_on_start.main()
    assert calls == ["head", "head"]
