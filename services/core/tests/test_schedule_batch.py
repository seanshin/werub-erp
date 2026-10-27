"""v1.302 — 일정 캘린더·배치 작업 관리(목록·이력·지금 실행·일시정지·결정·조치함)."""
import json
from datetime import date, datetime

import pytest

from erp.common import batch_jobs as bj


class FakeRedis:
    """batch_jobs가 쓰는 명령만(bytes 반환 — 실제 redis.asyncio 기본값과 같게)."""
    def __init__(self):
        self.kv, self.h, self.l = {}, {}, {}

    @staticmethod
    def _b(v):
        return v if isinstance(v, bytes) else str(v).encode()

    async def get(self, k): return self.kv.get(k)
    async def set(self, k, v, ex=None): self.kv[k] = self._b(v)
    async def delete(self, k): self.kv.pop(k, None)
    async def expire(self, k, s): return True
    async def hset(self, k, f, v): self.h.setdefault(k, {})[self._b(f)] = self._b(v)
    async def hgetall(self, k): return dict(self.h.get(k, {}))
    async def hkeys(self, k): return list(self.h.get(k, {}))
    async def hdel(self, k, *fs):
        for f in fs:
            self.h.get(k, {}).pop(self._b(f), None)
    async def lpush(self, k, v): self.l.setdefault(k, []).insert(0, self._b(v))
    async def rpush(self, k, v): self.l.setdefault(k, []).append(self._b(v))
    async def lpop(self, k):
        q = self.l.get(k) or []
        return q.pop(0) if q else None
    async def ltrim(self, k, a, b): self.l[k] = self.l.get(k, [])[a:b + 1]
    async def lrange(self, k, a, b): return self.l.get(k, [])[a:b + 1]


def test_catalog_covers_every_scheduled_job():
    """잡 목록 단일 소스 — 스케줄러에 등록된 잡은 모두 카탈로그에(새 잡이 이름·조작 규칙 없이 화면에 뜨지 않게)."""
    from erp.workers.scheduler import build_scheduler
    sched = build_scheduler()
    ids = {j.id for j in sched.get_jobs()}
    assert ids <= set(bj.CATALOG), ids - set(bj.CATALOG)
    assert not bj.CATALOG["heartbeat"][2] and not bj.CATALOG["access_log_flush"][2]
    desc = {j.id: bj.describe_trigger(j.trigger) for j in sched.get_jobs()}
    assert desc["backup"] == "매일 22:00" and desc["recurring"] == "매시 20분"
    assert desc["inbound_consume"] == "5분마다" and desc["sync_verify"] == "매주 일 03:00"
    assert desc["access_log_flush"] == "15초마다"


async def test_commands_run_pause_resume_and_history():
    """지금 실행(다음 실행=지금·수동 표시)·일시정지(재시작 후 재적용)·재개 · 조작 불가 잡 거부."""
    from erp.errors import ValidationError
    from erp.workers.scheduler import build_scheduler
    r = FakeRedis()
    with pytest.raises(ValidationError, match="응답이 없습니다"):          # 실행기 하트비트 없으면 명령 거부
        await bj.enqueue(r, op="pause", job_id="backup", reason="스토리지 점검", actor=7)
    import time
    await r.set(bj.HEARTBEAT_KEYS["worker-batch"], str(int(time.time())))
    sched = build_scheduler()
    sched.start(paused=True)
    try:
        with pytest.raises(ValidationError, match="할 수 없습니다"):
            await bj.enqueue(r, op="pause", job_id="heartbeat", reason="점검", actor=1)
        with pytest.raises(ValidationError, match="사유"):
            await bj.enqueue(r, op="pause", job_id="backup", reason="", actor=1)
        await bj.enqueue(r, op="pause", job_id="backup", reason="스토리지 점검", actor=7)
        await bj.enqueue(r, op="run", job_id="fee_sync", reason="수동 확인", actor=7)
        res = await bj.apply_commands(r, sched)
        assert [x["ok"] for x in res] == [True, True]
        assert sched.get_job("backup").next_run_time is None
        nrt = sched.get_job("fee_sync").next_run_time
        assert nrt is not None and (nrt - datetime.now(nrt.tzinfo)).total_seconds() < 5
        ov = await bj.overview(r)
        backup = next(j for j in ov["jobs"] if j["id"] == "backup")
        assert backup["paused"] and backup["paused_info"]["reason"] == "스토리지 점검" and backup["trigger"] == "매일 22:00"
        # 요청 전에 예정됐던 실행(이미 돌던 것)은 수동 표시를 가져가지 않는다 · 요청 뒤 예정된 실행에 붙는다
        await bj.record_run(r, "fee_sync", "ok", "", scheduled_ts=int(time.time()) - 600)
        assert (await bj.history(r, "fee_sync"))[0]["manual_by"] is None
        await bj.record_run(r, "fee_sync", "ok", "", scheduled_ts=int(time.time()))
        assert (await bj.history(r, "fee_sync"))[0]["manual_by"] == "7"
        await bj.record_run(r, "fee_sync", "ok", "")
        assert (await bj.history(r, "fee_sync"))[0]["manual_by"] is None
        # 실행 중이면 '지금 실행' 거부(동시 1실행에 조용히 버려지던 것)
        await bj.enqueue(r, op="run", job_id="his_sync", reason="수동 확인", actor=7)
        res = await bj.apply_commands(r, sched, running={"his_sync"})
        assert not res[0]["ok"] and "이미 실행 중" in res[0]["msg"]
        # 오래된 명령은 만료(실행기 정지 중 쌓인 명령이 늦게 돌지 않게)
        await r.rpush(bj.COMMANDS_KEY, json.dumps({"req": "x", "op": "run", "job": "his_sync", "by": 7,
                                                   "at": int(time.time()) - 3600}))
        res = await bj.apply_commands(r, sched)
        assert not res[0]["ok"] and "만료" in res[0]["msg"]
        # 기록은 일시정지인데 실제로 돌고 있으면(기동 시 재적용 실패) 매 주기 바로잡는다
        sched.get_job("backup").resume()
        await bj.publish_schedule(r, sched)
        assert next(j for j in (await bj.overview(r))["jobs"] if j["id"] == "backup")["pause_mismatch"]
        assert await bj.reconcile_paused(r, sched) == ["backup"]
        assert sched.get_job("backup").next_run_time is None
        # 재시작: 새 스케줄러에 일시정지 재적용
        s2 = build_scheduler()
        s2.start(paused=True)
        try:
            assert await bj.reapply_paused(r, s2) == ["backup"]
            assert s2.get_job("backup").next_run_time is None
        finally:
            s2.shutdown(wait=False)
        await bj.enqueue(r, op="resume", job_id="backup", reason="점검 끝", actor=7)
        await bj.apply_commands(r, sched)
        assert sched.get_job("backup").next_run_time is not None
        assert not next(j for j in (await bj.overview(r))["jobs"] if j["id"] == "backup")["paused_info"]
        assert "backup" not in {k.decode() for k in await r.hkeys(bj.PAUSED_KEY)}
    finally:
        sched.shutdown(wait=False)


async def test_action_center_batch_items(session, monkeypatch):
    from erp.app import action_center as ac
    from erp.common import redis_client
    r = FakeRedis()
    monkeypatch.setattr(redis_client, "get_redis", lambda: r)
    today = date(2026, 10, 11)
    assert not {a["key"] for a in (await ac.screen_actions(session, today=today))["actions"]} & {"batch-paused"}
    import time
    now = int(time.time())
    await r.set(bj.SCHEDULE_KEY, json.dumps({
        "backup": {"trigger": "매일 22:00", "next_run": None, "paused": True, "interval_s": 86400},
        "his_sync": {"trigger": "10분마다", "next_run": now + 60, "paused": False, "interval_s": 600},
        "fee_sync": {"trigger": "매일 22:30", "next_run": now + 60, "paused": False, "interval_s": 86400}}))
    await r.hset(bj.PAUSED_KEY, "backup", json.dumps({"by": 1, "reason": "점검", "at": now}))
    await r.hset(bj.STATUS_KEY, "his_sync", json.dumps({"ts": now - 7200, "status": "ok", "err": ""}))
    await r.hset(bj.STATUS_KEY, "fee_sync", json.dumps({"ts": now - 60, "status": "error", "err": "boom"}))
    got = {a["key"]: a for a in (await ac.screen_actions(session, today=today))["actions"]}
    assert "DB·첨부 백업" in got["batch-paused"]["title"]
    assert "수가 마스터" in got["batch-failed"]["title"] and "HIS 스테이징" in got["batch-late"]["title"]


async def test_batch_endpoints_rbac_and_audit(session, client, monkeypatch):
    from sqlalchemy import select

    from erp.audit.models import AuditLog
    from erp.common import redis_client
    from tests._http import auth_as
    import time
    r = FakeRedis()
    await r.set(bj.HEARTBEAT_KEYS["worker-batch"], str(int(time.time())))
    monkeypatch.setattr(redis_client, "get_redis", lambda: r)
    admin = await auth_as(session, user_id=1, role="ADMIN")
    director = await auth_as(session, user_id=2, role="DIRECTOR")
    ov = await client.get("/api/v1/admin/batch-jobs", headers=director)
    assert ov.status_code == 200 and ov.json()["redis_ok"] and any(j["id"] == "backup" for j in ov.json()["jobs"])
    assert (await client.post("/api/v1/admin/batch-jobs/backup/pause", headers=director,
                              json={"reason": "점검"})).status_code == 403
    ok = await client.post("/api/v1/admin/batch-jobs/backup/pause", headers=admin, json={"reason": "스토리지 점검"})
    assert ok.status_code == 200 and ok.json()["queued"]
    assert (await client.post("/api/v1/admin/batch-jobs/heartbeat/pause", headers=admin,
                              json={"reason": "점검"})).status_code == 422
    assert (await client.post("/api/v1/admin/batch-jobs/backup/delete", headers=admin,
                              json={"reason": "점검"})).status_code == 422
    assert (await client.get("/api/v1/admin/batch-jobs/nope/history", headers=admin)).status_code == 422
    assert await session.scalar(select(AuditLog).where(AuditLog.action == "BATCH_PAUSE"))
    assert json.loads(r.l[bj.COMMANDS_KEY][0])["by"] == 1


async def test_schedule_month_aggregates_sources(session, client):
    from erp.common.models import Holiday, RecurringTask
    from tests._http import auth_as
    session.add(RecurringTask(code="t.monthly", name="월 마감 점검", domain="finance", cadence="MONTHLY",
                              anchor={"basis": "PERIOD_END", "offset_days": 10, "hour": 9}, execution_mode="MANUAL"))
    session.add(RecurringTask(code="t.daily", name="매일 작업", domain="finance", cadence="DAILY", execution_mode="AUTO"))
    session.add(Holiday(on_date=date(2026, 9, 26), name="추석 연휴", kind="LUNAR"))
    await session.flush()
    h = await auth_as(session, user_id=1, role="ADMIN")
    r = await client.get("/api/v1/admin/schedule?yyyymm=2026-10", headers=h)
    assert r.status_code == 200, r.text
    ev = r.json()["events"]
    rec = [e for e in ev if e["kind"] == "recurring"]
    assert [(e["date"], e["title"], e["time"]) for e in rec] == [("2026-10-10", "월 마감 점검", "09:00")]   # 9월 말+10일
    hol = {e["date"]: e for e in ev if e["kind"] == "holiday"}
    assert hol["2026-10-03"]["title"] == "개천절" and hol["2026-10-05"]["status"].startswith("코드 표")
    assert hol["2026-10-09"]["title"] == "한글날"
    sep = (await client.get("/api/v1/admin/schedule?yyyymm=2026-09", headers=h)).json()["events"]
    assert any(e["date"] == "2026-09-26" and e["status"] == "캘린더 등록" for e in sep)
    may = (await client.get("/api/v1/admin/schedule?yyyymm=2026-05", headers=h)).json()["events"]
    assert any(e["date"] == "2026-05-01" and e["title"] == "노동절" for e in may)
    assert (await client.get("/api/v1/admin/schedule?yyyymm=2026-13", headers=h)).status_code == 422
    staff = await auth_as(session, user_id=3, role="STAFF")
    assert (await client.get("/api/v1/admin/schedule?yyyymm=2026-10", headers=staff)).status_code == 403
    assert r.json()["errors"] == []
