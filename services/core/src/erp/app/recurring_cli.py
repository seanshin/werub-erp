"""정기업무 AI CLI — Claude Code(운영자)가 '무엇이 밀렸나 → 안전한 것 실행 → 보고'를 처리.

컨테이너 내 실행(scripts/ 미포함이라 패키지 -m 진입):
    docker exec -w /app -e PYTHONPATH=/app/src infra-worker-batch-1 \
        python -m erp.app.recurring_cli due

서브커맨드(전부 구조화 JSON 출력):
  due                    현재 due/overdue 현황(ai_runnable 플래그 포함)
  run <code> [--force]   AUTO/AI_ASSISTED 단건 실행(requires_approval은 거부)
  run-due [--dry-run]    AUTO 일괄 실행(dry-run=섀도우·물질화만)
  report                 요약(총계·overdue·최근 FAILED)
  brief                  주간 운영 브리핑(최근 7일 실행·지연·이상 신호 — 핸들러와 동일 산출)

안전경계: AUTO만 자동실행, AI_ASSISTED/MANUAL/requires_approval은 서비스가 차단(기획서 §7).
실행 주체는 SYSTEM_ACTOR(0)·triggered_by=AI_CLI로 감사 기록된다.
"""
import argparse
import asyncio
import json
import sys

import erp.app.recurring_handlers  # noqa: F401 — AUTO 핸들러 등록(CLI 프로세스에도 필수)
from erp.app.recurring_service import SYSTEM_ACTOR, RecurringRunner
from erp.db import session_factory

_svc = RecurringRunner()


async def _due() -> dict:
    async with session_factory()() as session:
        rows = await _svc.due_list(session)
    return {"due": rows, "overdue": [r["code"] for r in rows if r["overdue"]]}


async def _run(code: str, force: bool) -> dict:
    async with session_factory()() as session:
        r = await _svc.run_one(session, code=code, actor=SYSTEM_ACTOR,
                               triggered_by="AI_CLI", force=force)
        return {"code": code, "period_key": r.period_key, "status": r.status, "error": r.error}


async def _run_due(dry_run: bool) -> dict:
    async with session_factory()() as session:
        return await _svc.run_due(session, dry_run=dry_run, actor=SYSTEM_ACTOR,
                                  triggered_by="AI_CLI")


async def _seed() -> dict:
    async with session_factory()() as session:
        before = (await _svc.registry_drift(session))["missing"]
        starter = await _svc.seed_starter_tasks(session)
        migrated = await _svc.seed_migrated_jobs(session)
        drift = await _svc.registry_drift(session)
    # 누락분만 등록 — 값이 다른 기존 업무는 관리화면 '정의 동기화'에서 관리자가 고른다
    return {"starter": starter, "migrated_jobs": migrated,
            "added": [m["code"] for m in before],
            "differs_left_for_admin": [d["code"] for d in drift["differs"]]}


async def _brief() -> dict:
    async with session_factory()() as session:
        return await _svc.weekly_briefing(session)


async def _report() -> dict:
    async with session_factory()() as session:
        rows = await _svc.due_list(session)
    by_status: dict[str, int] = {}
    for r in rows:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {"total": len(rows), "by_status": by_status,
            "overdue": [r["code"] for r in rows if r["overdue"]],
            "ai_runnable_due": [r["code"] for r in rows
                                if r["ai_runnable"] and r["status"] == "DUE"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="recurring", description="정기업무 AI CLI")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("due")
    pr = sub.add_parser("run")
    pr.add_argument("code")
    pr.add_argument("--force", action="store_true")
    prd = sub.add_parser("run-due")
    prd.add_argument("--dry-run", action="store_true")
    sub.add_parser("report")
    sub.add_parser("brief")
    sub.add_parser("seed")
    args = p.parse_args(argv)

    try:
        if args.cmd == "due":
            out = asyncio.run(_due())
        elif args.cmd == "run":
            out = asyncio.run(_run(args.code, args.force))
        elif args.cmd == "run-due":
            out = asyncio.run(_run_due(args.dry_run))
        elif args.cmd == "seed":
            out = asyncio.run(_seed())
        elif args.cmd == "brief":
            out = asyncio.run(_brief())
        else:
            out = asyncio.run(_report())
    except Exception as exc:  # noqa: BLE001 — AI가 파싱하도록 오류도 구조화 JSON으로
        print(json.dumps({"error": str(exc)[:200], "cmd": args.cmd}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
