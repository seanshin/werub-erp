"""공유 PG 연결 슬롯 진단 — admin 온디맨드 엔드포인트와 이웃감시 잡(P2-4)이 공유하는 순수 통계.

공유 박스(HIS·OpenPACS 동거, max_connections=200)에서 OpenPACS 유휴 누수로 슬롯 고갈→HIS SSO 500
반복 인시던트의 조기경보. 타 테넌트 state/idle-age는 erp_app 비특권이라 가려짐(NULL) — usename별
연결수로 점유만 관측. 회복(openpacs idle>60m 종료)은 superuser·HIS팀 영역(ops-runbook §9.1).
"""
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from erp.config import get_settings


async def shared_pg_stats(session: AsyncSession) -> dict | None:
    """pg_stat_activity 기반 공유 PG 슬롯 점유 통계. sqlite(단위테스트)는 None."""
    if session.bind.dialect.name != "postgresql":
        return None
    rows = (await session.execute(text(
        "SELECT coalesce(usename, '-') AS usename, state, count(*) AS cnt, "
        "coalesce(extract(epoch FROM max(now()-state_change))::int, 0) AS max_idle_s "
        "FROM pg_stat_activity GROUP BY 1, 2"))).all()
    max_conn = int(await session.scalar(text(
        "SELECT setting FROM pg_settings WHERE name = 'max_connections'")) or 0)
    tenants: dict[str, dict] = {}
    total = 0
    for usename, state, cnt, max_idle_s in rows:
        total += cnt
        t = tenants.setdefault(usename, {"usename": usename, "connections": 0,
                                         "states": {}, "max_idle_seconds": 0,
                                         "state_visible": False})
        t["connections"] += cnt
        if state is not None:                 # 자기(erp_app) 세션만 state/idle 가시
            t["state_visible"] = True
            t["states"][state] = t["states"].get(state, 0) + cnt
            t["max_idle_seconds"] = max(t["max_idle_seconds"], int(max_idle_s or 0))
    by_tenant = sorted(tenants.values(), key=lambda x: x["connections"], reverse=True)
    usage_pct = round(total / max_conn * 100) if max_conn else None
    warn_pct = get_settings().pg_neighbor_warn_pct
    return {
        "max_connections": max_conn, "total": total, "usage_pct": usage_pct,
        "warn_pct": warn_pct,
        "warning": bool(usage_pct is not None and usage_pct >= warn_pct),
        "by_tenant": by_tenant,
        "note": "타 테넌트 state/idle은 erp_app 비특권으로 가려짐(NULL) — usename별 연결수로 "
                "슬롯 점유 조기경보. 회복(openpacs idle>60m 종료)=superuser·HIS팀, ops-runbook §9.1.",
    }
