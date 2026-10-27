"""e-sign 사번 매핑 백필(HIS (나)·2026-07-24) — 디렉터리 조인·서명자 우선·map 발신·dry_run."""
from datetime import date

import erp.integrations.esign_emp_map as em
from erp.hr.models import Employee, MedicalStaffContract


async def _emp(session, no, ext, resigned=None):
    e = Employee(employee_no=no, name=f"직원{no}", external_user_id=ext,
                 hired_on=date(2025, 1, 1), job_group="DOCTOR",
                 employment_type="REGULAR", source="ERP", resigned_on=resigned)
    session.add(e)
    await session.flush()
    return e


def _mock_http(monkeypatch, directory, posts):
    class _Resp:
        def __init__(self, body=None):
            self._b = body or {}
            self.status_code = 200      # _post_map이 상태코드로 재시도를 판단한다
            self.headers = {}

        def raise_for_status(self): pass
        def json(self): return self._b

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            return _Resp({"data": directory})
        async def post(self, url, json=None, headers=None):
            posts.append({"url": url, "body": json})
            return _Resp({})
    monkeypatch.setattr(em.httpx, "AsyncClient", _Client)


async def test_backfill_signatories_join_and_map(session, monkeypatch):
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    # 서명자(의료계약 ACTIVE) 1명 + 비서명자 1명
    sig = await _emp(session, "E0001", "clinic-11")
    await _emp(session, "E0002", "clinic-22")   # 서명자 아님
    session.add(MedicalStaffContract(employee_id=sig.id, valid_from=date(2026, 1, 1),
                                     base_pay=9_000_000, status="ACTIVE",
                                     incentive_type="NONE"))
    await session.flush()
    directory = [{"clinicUserId": "clinic-11", "employeeNumber": "BF-2026-0001"},
                 {"clinicUserId": "clinic-22", "employeeNumber": "BF-2026-0002"}]
    posts: list = []
    _mock_http(monkeypatch, directory, posts)
    try:
        s.his_webhook_key = "wk"
        out = await em.backfill(session, actor=1, signatories_only=True)
        # 서명자 1명만 대상·map 1건(BF-2026-0001·erpEmployeeCode=E0001)
        assert out["candidates"] == 1 and out["matched"] == 1 and out["mapped"] == 1
        assert len(posts) == 1
        assert posts[0]["url"].endswith("/hr/staff/BF-2026-0001/map")
        assert posts[0]["body"] == {"erpEmployeeCode": "E0001"}
    finally:
        s.his_webhook_key = old


async def test_backfill_dry_run_no_post(session, monkeypatch):
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    await _emp(session, "E0009", "clinic-99")
    directory = [{"clinicUserId": "clinic-99", "employeeNumber": "BF-2026-0009"}]
    posts: list = []
    _mock_http(monkeypatch, directory, posts)
    try:
        s.his_webhook_key = "wk"
        out = await em.backfill(session, actor=1, signatories_only=False, dry_run=True)
        assert out["matched"] == 1 and out["mapped"] == 0 and posts == []   # 조인만·발신 없음
    finally:
        s.his_webhook_key = old


async def test_backfill_no_key_skips(session):
    from erp.config import get_settings
    s = get_settings()
    old_wk, old_pii = s.his_webhook_key, s.his_pii_read_key
    try:
        s.his_webhook_key, s.his_pii_read_key = None, None
        out = await em.backfill(session, actor=1)
        assert "skipped" in out
    finally:
        s.his_webhook_key, s.his_pii_read_key = old_wk, old_pii


# ---- HIS → ERP 역방향(B-2): external_user_id 백필 ----

async def test_external_ids_dry_run_then_apply(session, monkeypatch):
    """사번으로 조인해 빈 external_user_id를 채운다. dry_run은 DB를 건드리지 않는다."""
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "k"
    try:
        e1 = await _emp(session, "E0100", None)
        e2 = await _emp(session, "E0101", None)
        _mock_http(monkeypatch, [
            {"employeeNumber": "E0100", "clinicUserId": "clinic-100"},
            {"employeeNumber": "E0101", "clinicUserId": "clinic-101"}], [])

        out = await em.backfill_external_ids(session, dry_run=True)
        assert out["planned"] == 2 and out["applied"] == 0
        assert e1.external_user_id is None and e2.external_user_id is None

        out = await em.backfill_external_ids(session, dry_run=False)
        assert out["applied"] == 2
        assert e1.external_user_id == "clinic-100" and e2.external_user_id == "clinic-101"
    finally:
        s.his_webhook_key = old


async def test_external_ids_never_overwrite_or_duplicate(session, monkeypatch):
    """이미 값이 있는 직원은 건드리지 않고, 한 clinicUserId가 둘에 붙지 않는다(1:1 유지)."""
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "k"
    try:
        held = await _emp(session, "E0200", "clinic-200")   # 이미 연결됨
        dup = await _emp(session, "E0201", None)            # 디렉터리가 같은 id를 가리킴
        ok = await _emp(session, "E0202", None)
        _mock_http(monkeypatch, [
            {"employeeNumber": "E0200", "clinicUserId": "clinic-999"},   # 덮어쓰기 시도
            {"employeeNumber": "E0201", "clinicUserId": "clinic-200"},   # 중복 시도
            {"employeeNumber": "E0202", "clinicUserId": "clinic-202"}], [])

        out = await em.backfill_external_ids(session, dry_run=False)
        assert held.external_user_id == "clinic-200"       # 불변
        assert dup.external_user_id is None                # 중복 배제
        assert ok.external_user_id == "clinic-202"
        assert out["skipped_duplicate_clinic_id"] == 1 and out["applied"] == 1
    finally:
        s.his_webhook_key = old


async def test_external_ids_limit_supports_single_trial(session, monkeypatch):
    """HIS 제안대로 1명 시험 → 배치. limit=1이면 딱 1건만 적용된다."""
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "k"
    try:
        await _emp(session, "E0300", None)
        await _emp(session, "E0301", None)
        _mock_http(monkeypatch, [
            {"employeeNumber": "E0300", "clinicUserId": "clinic-300"},
            {"employeeNumber": "E0301", "clinicUserId": "clinic-301"}], [])
        out = await em.backfill_external_ids(session, dry_run=False, limit=1)
        assert out["applied"] == 1
    finally:
        s.his_webhook_key = old


# ---- 상대 스로틀 대응(v1.278) ----

async def test_map_retries_on_throttle(session, monkeypatch):
    """429는 백오프 재시도한다 — 78건 연속 POST로 **전건 429 실패**한 적이 있다."""
    from erp.config import get_settings
    s = get_settings()
    old = s.his_webhook_key
    s.his_webhook_key = "k"
    calls = []

    class _Resp:
        def __init__(self, code):
            self.status_code = code
            self.headers = {}
        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")
        def json(self): return {}

    class _Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def get(self, url, params=None, headers=None):
            return type("R", (), {"raise_for_status": lambda s: None,
                                  "json": lambda s: {"data": [
                                      {"employeeNumber": "E1", "clinicUserId": "c1"}]}})()
        async def post(self, url, json=None, headers=None):
            calls.append(url)
            return _Resp(429 if len(calls) < 3 else 200)   # 두 번 스로틀 후 성공

    try:
        monkeypatch.setattr(em.httpx, "AsyncClient", _Client)
        monkeypatch.setattr(em.asyncio, "sleep", lambda *_a: _noop())
        await _emp(session, "E1", "c1")
        out = await em.backfill(session, signatories_only=False)
        assert out["mapped"] == 1 and out["failed"] == 0
        assert len(calls) == 3                              # 429 두 번 → 세 번째 성공
        assert out["final_pace_s"] > 0.35                   # 429를 만나 속도를 늦췄다
    finally:
        s.his_webhook_key = old


async def _noop():
    return None
