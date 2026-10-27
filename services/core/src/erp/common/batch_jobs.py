"""배치 작업(worker-batch·APScheduler) 관리 — 목록·일정·이력·지금 실행·일시정지 (v1.302.0·2026-10-11).

종전엔 잡별 '마지막 상태' 1건(Redis·7일)만 있고 이력·다음 실행 시각·조작 수단이 없었다 — 장애 대응·점검 때 컨테이너를 내려야 했다.

구조: core(화면·API)와 worker-batch(스케줄러 리더)가 **같은 Redis 키**로만 주고받는다(core는 스케줄러 객체에 닿지 않는다).
- 스케줄러 → Redis: 일정(주기 표기·다음 실행·일시정지)·실행 이력(잡별 최근 50건)·명령 처리 결과.
- core → Redis: 명령 큐(지금 실행·일시정지·재개 — 사유·요청자). 리더가 30초마다 꺼내 적용한다.
- 일시정지는 Redis 해시에 남겨 재시작해도 다시 적용한다.
키·잡 목록은 이 모듈이 단일 소스다(양쪽이 문자열을 따로 적지 않는다).
"""
from __future__ import annotations

import json
import time
import uuid

STATUS_KEY = "erp:infrajobs:status"          # 잡별 마지막 상태(기존·R7)
SCHEDULE_KEY = "erp:infrajobs:schedule"      # 잡별 주기·다음 실행·일시정지(30초마다 갱신)
HISTORY_KEY = "erp:infrajobs:history:{job}"  # 잡별 실행 이력(최근 50건)
PAUSED_KEY = "erp:infrajobs:paused"          # 일시정지 잡 → {by, reason, at}(재시작 후 재적용)
COMMANDS_KEY = "erp:infrajobs:commands"      # core → 리더 명령 큐
RESULTS_KEY = "erp:infrajobs:command_results"
MANUAL_KEY = "erp:infrajobs:manual:{job}"    # 다음 실행이 수동 요청임을 표시(이력에 남김)
HEARTBEAT_KEYS = {"worker-batch": "erp:heartbeat:worker-batch",
                  "worker-integration": "erp:heartbeat:worker-integration"}
# 하트비트 '멈춤 의심' 기준(초) — 연동 실행기는 설계상 최대 600초까지 비울 수 있다(TTL 900)
HEARTBEAT_LIMITS = {"worker-batch": 180, "worker-integration": 900}
HISTORY_MAX = 50
TTL_S = 30 * 86400

# 잡 목록 단일 소스 — id → (이름, 설명, 일시정지 가능, 지금 실행 가능)
# 하트비트·접속기록 적재·조작 반영은 멈추면 딥 헬스·접속기록 내구성·조작 자체가 깨진다 → 조작 불가.
CATALOG: dict[str, tuple[str, str, bool, bool]] = {
    "fee_sync": ("수가 마스터 동기화", "공공데이터 수가 마스터 반영(키 없으면 대기)", True, True),
    "appeal_deadline": ("이의신청 기한 알림", "법정 기한 D-14·7·3 알림", True, True),
    "attendance_summary": ("근태 수집·집계", "그룹웨어 근태 수집 → 월 집계", True, True),
    "clinic_staff_sync": ("Clinic 직원 동기화", "Clinic 직원 → 인사 직원", True, True),
    "backup": ("DB·첨부 백업", "pg_dump + 첨부 tar·암호화·보관 주기·오프사이트", True, True),
    "heartbeat": ("실행기 생존 신호", "딥 헬스가 보는 신호(60초)", False, False),
    "pg_neighbor_watch": ("공유 PG 슬롯 감시", "연결 슬롯 압박 시 관리자 경보", True, True),
    "access_log_flush": ("접속기록 적재", "접속기록 큐 → 감사 테이블(15초·개인정보 보호법)", False, False),
    "parse_notices": ("공지 파싱", "공지 스테이징 파싱", True, True),
    "collect_drops": ("파일 드롭 수집", "드롭 폴더 → 스테이징", True, True),
    "his_sync": ("HIS 스테이징 반영", "환자·수납 스테이징 반영", True, True),
    "inbound_consume": ("HIS 수신함 소비", "웹훅 수신 이벤트 → 업무 반영(실시간 소비의 안전망)", True, True),
    "settlement_callbacks": ("정산 콜백 발송", "settlement.paid 발신함 발송", True, True),
    "esign_fetch": ("전자서명 문서 수집", "서명 완료 문서 수집 큐", True, True),
    "sync_verify": ("동기화 검증", "주간 동기화 검증(현재 기록만)", True, True),
    "recurring": ("정기업무 실행", "켜진 자동 정기업무 실행·수동 업무 생성·AI 초안 반영(매시 20분)", True, True),
    "batch_control": ("배치 조작 반영", "이 화면의 지금 실행·일시정지 명령 반영(30초)", False, False),
}


def _s(v) -> str | None:
    return v.decode() if isinstance(v, bytes) else v


def describe_trigger(trigger) -> str:
    """APScheduler 트리거 → 사람이 읽는 주기('매일 22:30'·'10분마다'·'매주 일 03:00'·'매시 20분')."""
    iv = getattr(trigger, "interval", None)
    if iv is not None:
        sec = int(iv.total_seconds())
        return (f"{sec // 3600}시간마다" if sec % 3600 == 0 and sec >= 3600
                else f"{sec // 60}분마다" if sec % 60 == 0 and sec >= 60 else f"{sec}초마다")
    fields = {f.name: str(f) for f in getattr(trigger, "fields", [])}
    if not fields:
        return str(trigger)
    hour, minute, dow = fields.get("hour", "*"), fields.get("minute", "*"), fields.get("day_of_week", "*")
    days = {"mon": "월", "tue": "화", "wed": "수", "thu": "목", "fri": "금", "sat": "토", "sun": "일"}
    if hour == "*" and minute.isdigit():
        return f"매시 {int(minute)}분"
    if hour.isdigit() and minute.isdigit():
        at = f"{int(hour):02d}:{int(minute):02d}"
        return f"매주 {days.get(dow, dow)} {at}" if dow != "*" else f"매일 {at}"
    return str(trigger)


# ── worker-batch(리더) 쪽 ────────────────────────────────────────────────────────────
async def record_run(r, job_id: str, status: str, err: str, *, scheduled_ts: int | None = None) -> None:
    """실행 이벤트 → 이력(최근 50건). 수동 요청 표시는 **요청 시각 이후로 예정된 실행**에만 붙인다.

    요청 직전에 이미 돌던 실행이 끝나며 표시를 가져가 '수동 실행'으로 잘못 남던 것(검토 MED)을 막는다.
    """
    mk = MANUAL_KEY.format(job=job_id)
    raw = _s(await r.get(mk))          # GETDEL은 Redis 6.2+ — 공유 Redis 버전에 기대지 않는다
    manual = None
    if status == "skipped":
        # 건너뜀은 수동 요청이 걸려 있을 때만 남긴다(늘 주기를 넘기는 잡의 건너뜀이 이력 50건을 밀어내지 않게)·표시는 가져가지 않는다
        # (수동 실행이 길어 다음 예정 실행이 건너뛰어진 경우 — 표시는 진짜 수동 실행에 남아야 한다)
        if raw is None:
            return
        err = err or "실행 중이라 이번 예정 실행을 건너뜀"
    elif raw is not None:
        try:
            flag = json.loads(raw)
        except ValueError:
            flag = {"by": raw, "at": 0}
        if scheduled_ts is None or scheduled_ts >= int(flag.get("at", 0)) - 1:
            manual = str(flag.get("by"))
            await r.delete(mk)
    key = HISTORY_KEY.format(job=job_id)
    await r.lpush(key, json.dumps({"ts": int(time.time()), "status": status, "err": err[:120],
                                   "manual_by": manual}, ensure_ascii=False))
    await r.ltrim(key, 0, HISTORY_MAX - 1)
    await r.expire(key, TTL_S)


def _interval_s(trigger) -> int | None:
    """기대 발화 간격(초) — 다음 두 발화 시각 차. 'N배 넘게 안 돎' 판정 기준(30초마다 다시 게시 — 7일 TTL로 사라지던 것 보완)."""
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Asia/Seoul"))
        t1 = trigger.get_next_fire_time(None, now)
        t2 = trigger.get_next_fire_time(t1, t1) if t1 else None
        return int((t2 - t1).total_seconds()) if t1 and t2 else None
    except Exception:  # noqa: BLE001 — 관측 보조값
        return None


async def publish_schedule(r, sched) -> None:
    """잡별 주기·다음 실행 시각·일시정지 게시(30초마다) — 다음 실행 None = 일시정지."""
    out = {}
    for j in sched.get_jobs():
        nrt = getattr(j, "next_run_time", None)
        out[j.id] = {"trigger": describe_trigger(j.trigger), "next_run": int(nrt.timestamp()) if nrt else None,
                     "paused": nrt is None, "interval_s": _interval_s(j.trigger)}
    await r.set(SCHEDULE_KEY, json.dumps(out, ensure_ascii=False), ex=TTL_S)


async def reapply_paused(r, sched) -> list[str]:
    """기동 시 — 일시정지 기록이 있는 잡을 다시 멈춘다(재시작으로 조용히 재개되지 않게)."""
    done = []
    for k in (await r.hkeys(PAUSED_KEY)) or []:
        jid = _s(k)
        job = sched.get_job(jid)
        if job is not None and CATALOG.get(jid, ("", "", False, False))[2]:
            job.pause()
            done.append(jid)
    return done


COMMAND_MAX_AGE_S = 600     # 실행기가 내려가 있던 동안 쌓인 명령은 늦게(진료 시간 등) 반영되지 않게 만료


async def reconcile_paused(r, sched) -> list[str]:
    """일시정지 기록이 있는데 실제로는 돌고 있는 잡을 다시 멈춘다 — 매 주기(기동 시 재적용이 실패해도 30초 안에 바로잡힘·검토 MED)."""
    fixed = []
    for k in (await r.hkeys(PAUSED_KEY)) or []:
        jid = _s(k)
        job = sched.get_job(jid)
        if job is not None and job.next_run_time is not None and CATALOG.get(jid, ("", "", False, False))[2]:
            job.pause()
            fixed.append(jid)
    return fixed


async def apply_commands(r, sched, *, running: set[str] | None = None, limit: int = 20) -> list[dict]:
    """명령 큐 처리 — 지금 실행(다음 실행=지금)·일시정지·재개. 카탈로그가 막은 조작은 거부(이중 방어).

    running: 지금 실행 중인 잡 id — 실행 중이면 '지금 실행'을 거부한다(동시 1실행이라 요청이 조용히 버려졌다·검토 MED).
    COMMAND_MAX_AGE_S보다 오래된 명령은 '만료'(실행기 정지 중 쌓인 백업 실행 등이 다음 날 아침에 돌지 않게).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo
    running = running or set()
    results = []
    for _ in range(limit):
        raw = _s(await r.lpop(COMMANDS_KEY))
        if raw is None:
            break
        cmd = json.loads(raw)
        op, jid = cmd.get("op"), cmd.get("job")
        meta = CATALOG.get(jid)
        job = sched.get_job(jid)
        ok, msg = False, ""
        if int(time.time()) - int(cmd.get("at", 0)) > COMMAND_MAX_AGE_S:
            msg = "만료 — 실행기가 늦게 받았습니다(다시 요청하세요)"
        elif meta is None or job is None:
            msg = "등록되지 않은 잡"
        elif op == "run" and meta[3]:
            if job.next_run_time is None:
                msg = "일시정지 중 — 재개 후 실행하세요"
            elif jid in running:
                msg = "이미 실행 중 — 끝난 뒤 다시 요청하세요"
            else:
                now = datetime.now(ZoneInfo("Asia/Seoul"))
                await r.set(MANUAL_KEY.format(job=jid), json.dumps({"by": cmd.get("by"), "at": int(now.timestamp())}),
                            ex=3600)
                job.modify(next_run_time=now)
                ok, msg = True, "실행 요청됨"
        elif op == "pause" and meta[2]:
            await r.hset(PAUSED_KEY, jid, json.dumps({"by": cmd.get("by"), "reason": cmd.get("reason"),
                                                      "at": int(time.time())}, ensure_ascii=False))
            job.pause()                    # 기록 먼저 — 기록이 실패하면 멈추지 않는다(재시작 후 조용히 재개 방지)
            ok, msg = True, "일시정지됨"
        elif op == "resume" and meta[2]:
            await r.hdel(PAUSED_KEY, jid)  # 기록 먼저 지운다 — 지우기가 실패하면 재개하지 않는다(다음 주기 재정지·결과 유실 방지)
            job.resume()
            ok, msg = True, "재개됨"
        else:
            msg = "이 잡은 그 조작을 할 수 없습니다"
        res = {"req": cmd.get("req"), "op": op, "job": jid, "ok": ok, "msg": msg, "at": int(time.time())}
        results.append(res)
        await r.lpush(RESULTS_KEY, json.dumps(res, ensure_ascii=False))
        await r.ltrim(RESULTS_KEY, 0, 99)
    if results:
        await publish_schedule(r, sched)
    return results


# ── core(화면·API) 쪽 ───────────────────────────────────────────────────────────────
async def overview(r) -> dict:
    """잡 목록 = 카탈로그 × 게시된 일정 × 마지막 상태 × 일시정지 기록 + 실행기 하트비트."""
    now = int(time.time())
    sched = json.loads(_s(await r.get(SCHEDULE_KEY)) or "{}")
    status = {_s(k): json.loads(_s(v)) for k, v in ((await r.hgetall(STATUS_KEY)) or {}).items()}
    paused = {_s(k): json.loads(_s(v)) for k, v in ((await r.hgetall(PAUSED_KEY)) or {}).items()}
    beats = {}
    for name, key in HEARTBEAT_KEYS.items():
        v = _s(await r.get(key))
        beats[name] = (now - int(float(v))) if v else None
    jobs = []
    for jid in sorted(set(CATALOG) | set(sched)):
        label, desc, can_pause, can_run = CATALOG.get(jid, (jid, "카탈로그에 없는 잡", False, False))
        sc, st = sched.get(jid) or {}, status.get(jid) or {}
        actual = bool(sc.get("paused")) if sc else jid in paused     # 게시된 실제 상태가 기준(없으면 기록)
        jobs.append({"id": jid, "label": label, "description": desc, "can_pause": can_pause, "can_run": can_run,
                     "registered": jid in sched, "trigger": sc.get("trigger"), "next_run": sc.get("next_run"),
                     "interval_s": sc.get("interval_s"),
                     "paused": actual, "paused_info": paused.get(jid),
                     "pause_mismatch": bool(sc) and actual != (jid in paused),
                     "last_run": st.get("ts"), "last_status": st.get("status"), "last_error": st.get("err") or None})
    jobs.sort(key=lambda j: (not j["registered"], j["label"]))      # 이름순(id순은 한글 화면에서 무작위처럼 보였다)
    results = [json.loads(_s(x)) for x in (await r.lrange(RESULTS_KEY, 0, 19)) or []]
    pending = [json.loads(_s(x)) for x in (await r.lrange(COMMANDS_KEY, 0, 19)) or []]
    return {"now": now, "heartbeats": beats, "heartbeat_limits": HEARTBEAT_LIMITS, "schedule_published": bool(sched),
            "jobs": jobs, "recent_commands": results, "pending_commands": pending}


async def history(r, job_id: str) -> list[dict]:
    return [json.loads(_s(x)) for x in (await r.lrange(HISTORY_KEY.format(job=job_id), 0, HISTORY_MAX - 1)) or []]


async def enqueue(r, *, op: str, job_id: str, reason: str, actor: int) -> dict:
    """명령 등록 — 카탈로그 검사(조작 불가 잡 거부). 리더가 30초 안에 반영한다."""
    from erp.errors import ValidationError
    meta = CATALOG.get(job_id)
    if meta is None:
        raise ValidationError("등록되지 않은 배치 작업입니다", {"job": job_id})
    if op not in ("run", "pause", "resume"):
        raise ValidationError("알 수 없는 조작입니다", {"op": op})
    if (op == "run" and not meta[3]) or (op in ("pause", "resume") and not meta[2]):
        raise ValidationError(f"'{meta[0]}'은(는) {'지금 실행' if op == 'run' else '일시정지·재개'}할 수 없습니다 — "
                              "멈추면 상태 점검·접속기록 보관·조작 자체가 깨지는 작업입니다")
    why = (reason or "").strip()
    if len(why) < 4:
        raise ValidationError("사유를 4자 이상 입력하세요")
    hb = _s(await r.get(HEARTBEAT_KEYS["worker-batch"]))
    if hb is None or int(time.time()) - int(float(hb)) > HEARTBEAT_LIMITS["worker-batch"]:
        raise ValidationError("배치 실행기(worker-batch) 응답이 없습니다 — 명령이 반영되지 않습니다. 실행기 상태를 먼저 확인하세요")
    req = uuid.uuid4().hex[:12]
    await r.rpush(COMMANDS_KEY, json.dumps({"req": req, "op": op, "job": job_id, "by": actor, "reason": why[:200],
                                            "at": int(time.time())}, ensure_ascii=False))
    await r.expire(COMMANDS_KEY, 86400)
    return {"req": req, "op": op, "job": job_id, "queued": True}
