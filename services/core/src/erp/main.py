"""FastAPI 앱 팩토리 — design §2.1/§6.6 (보안 헤더 단일 소스)."""
import uuid

from fastapi import Cookie, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from erp.config import get_settings
from erp.db import get_session
from erp.errors import ERPException
from erp.logging import request_id_var, setup_logging


def _landing_html(status: str, prefix: str, authed: bool = False) -> str:
    """SSO 랜딩 HTML — CSP 준수(인라인 스크립트/스타일 없음).

    authed면 ERP 대시보드(web UI) 진입 링크를 제공.
    """
    nav = (f"<p><a href=\"{prefix}/dashboard\">ERP 대시보드 →</a></p>" if authed
           else f"<p><a href=\"{prefix}/api/docs\">API 문서</a></p>")
    return (
        "<!doctype html><html lang=\"ko\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        "<title>병원 ERP</title></head><body>"
        "<h1>병원 ERP</h1>"
        f"<p>{status}</p>"
        f"{nav}"
        "</body></html>"
    )


def _clear_session_cookies(resp, request: Request) -> None:
    """ERP 세션 쿠키 3종 삭제 — set_session_cookies와 같은 경로로(경로가 다르면 안 지워진다)."""
    prefix = request.headers.get("x-forwarded-prefix", "").rstrip("/")
    resp.delete_cookie("access_token", path=f"{prefix}/" or "/")
    resp.delete_cookie("refresh_token", path=f"{prefix}/api/v1/auth")
    resp.delete_cookie("csrf_token", path=f"{prefix}/" or "/")
    resp.delete_cookie("his_sso", path=prefix or "/erp")     # 실패한 핸드오프 쿠키 재소비 방지


def create_app() -> FastAPI:
    setup_logging()
    # 법정 파라미터 관리화면 등록분 — 요청 세션 훅 등록(60초 TTL 갱신·라우터는 지연 import라 여기서)
    import erp.hr.statutory  # noqa: F401
    # 첨부 공유 볼륨 권한 정비(core가 소유자 — worker-batch가 기존 샤드에도 쓸 수 있게)
    from erp.common.attachment_service import ensure_shared_permissions
    try:
        ensure_shared_permissions(get_settings().attachment_dir)
    except OSError:      # 권한 정비 실패가 기동을 막지 않는다(첨부 쓰기 실패로 드러난다)
        pass
    # API 전역 datetime 직렬화 — PG timestamptz(UTC aware)를 KST로 환산해 내보냄(전 화면 -9h 표시
    # 왜곡 방지·v1.259.1). 전 라우터가 dict 반환(response_model 미사용)이라 이 인코더가 전부 커버.
    from datetime import datetime as _dt

    from fastapi import encoders as _fenc

    from erp.common.kst import api_datetime_encoder, assert_process_tz_kst
    assert_process_tz_kst()   # naive KST → timestamptz 쓰기 정합의 전제(불일치=기동 중단)
    _fenc.ENCODERS_BY_TYPE[_dt] = api_datetime_encoder
    app = FastAPI(title="hospital-erp core", docs_url="/api/docs", openapi_url="/api/openapi.json")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        import time as _pt

        from erp.app import metrics as _metrics
        request_id_var.set(request.headers.get("x-request-id") or str(uuid.uuid4()))
        _start = _pt.perf_counter()
        response = await call_next(request)
        # 요청 메트릭 계측(P2-3) — 라우트 템플릿으로 라벨 카디널리티 제한
        _route = request.scope.get("route")
        _path = getattr(_route, "path_format", None) or getattr(_route, "path", None) or "unmatched"
        if _path != "/api/metrics":  # 스크레이프 자기계측 제외
            _metrics.record(request.method, _path, response.status_code, _pt.perf_counter() - _start)
        # 보안 헤더 (design §6.6 — FastAPI 단일 소스)
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'; object-src 'none'; base-uri 'self'"
        )
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Request-Id"] = request_id_var.get()
        return response

    @app.exception_handler(ERPException)
    async def erp_exception_handler(_: Request, exc: ERPException):
        # 스택트레이스 미노출 (design §4.1)
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
        )

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(_: Request, exc: RequestValidationError):
        # 422도 표준 {"error": …} 포맷으로 — FE(api.ts)가 body.error.message를 토스트에 노출.
        # 기본 {"detail":[…]}는 FE에서 "요청 처리에 실패했습니다"로 뭉개지던 결함(v1.203).
        fields = []
        for e in exc.errors()[:5]:
            loc = ".".join(str(p) for p in e.get("loc", []) if p not in ("body", "query", "path"))
            fields.append(loc or "요청")
        msg = f"입력값 오류: {', '.join(dict.fromkeys(fields))}" if fields else "입력값 오류"
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "VALIDATION-422", "message": msg,
                               "details": {"fields": fields}}},
        )

    from erp.app.admin_router import router as admin_router
    from erp.app.approval_router import router as approval_router
    from erp.app.compliance_router import router as compliance_router
    import erp.app.recurring_handlers  # noqa: F401 — 정기업무 AUTO 핸들러 등록(has_handler)
    from erp.app.recurring_router import router as recurring_router
    from erp.app.export_router import router as export_router
    from erp.app.month_end_router import router as month_end_router
    from erp.claims.router import router as claims_router
    from erp.common.router import router as common_router
    from erp.finance.router import router as finance_router
    from erp.hr.router import router as hr_router
    from erp.patient.router import router as patient_router
    from erp.regulatory.router import router as regulatory_router
    from erp.scm.router import router as scm_router

    app.include_router(common_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(approval_router, prefix="/api/v1")
    app.include_router(hr_router, prefix="/api/v1")
    app.include_router(regulatory_router, prefix="/api/v1")
    app.include_router(patient_router, prefix="/api/v1")
    app.include_router(claims_router, prefix="/api/v1")
    app.include_router(scm_router, prefix="/api/v1")
    app.include_router(finance_router, prefix="/api/v1")
    app.include_router(month_end_router, prefix="/api/v1")
    app.include_router(compliance_router, prefix="/api/v1")
    app.include_router(recurring_router, prefix="/api/v1")
    app.include_router(export_router, prefix="/api/v1")

    from erp.costing.router import router as costing_router
    app.include_router(costing_router, prefix="/api/v1")

    from erp.analytics.router import router as analytics_router
    app.include_router(analytics_router, prefix="/api/v1")

    from erp.tax.router import router as tax_router
    app.include_router(tax_router, prefix="/api/v1")

    from erp.itam.router import router as itam_router
    app.include_router(itam_router, prefix="/api/v1")

    from erp.integrations.eapproval_router import router as eapproval_router
    app.include_router(eapproval_router, prefix="/api/v1")

    from erp.integrations.his_webhook_router import router as his_webhook_router
    app.include_router(his_webhook_router, prefix="/api/v1")

    from erp.integrations.lis_webhook_router import router as lis_webhook_router
    app.include_router(lis_webhook_router, prefix="/api/v1")

    from erp.integrations.sign_webhook_router import router as sign_webhook_router
    app.include_router(sign_webhook_router, prefix="/api/v1")

    from erp.integrations.his_sign_origination import router as his_sign_origination_router
    app.include_router(his_sign_origination_router, prefix="/api/v1")

    from erp.integrations.bi_sso import router as bi_sso_router
    app.include_router(bi_sso_router, prefix="/api/v1")

    from erp.integrations.ess_router import router as ess_router
    app.include_router(ess_router, prefix="/api/v1")

    from erp.integrations.billing_api import router as billing_api_router
    app.include_router(billing_api_router, prefix="/api/v1")

    from erp.integrations.regulatory_api import router as regulatory_api_router
    app.include_router(regulatory_api_router, prefix="/api/v1")

    from erp.common.attachment_router import router as attachment_router
    app.include_router(attachment_router, prefix="/api/v1")

    @app.get("/api/health")
    async def health():
        return {"status": "ok", "env": get_settings().env}

    @app.get("/api/health/deep")
    async def health_deep(session: AsyncSession = Depends(get_session)):
        """심층 헬스 — DB·Redis·worker-batch 하트비트 검사(죽은 워커·의존성 감지, P0-2).

        얕은 /api/health는 상수 200이라 DB 다운·워커 사망을 못 잡음. 이 EP는 실제 의존성을
        점검해 이상 시 503 반환(외부 모니터/알람이 감지). 하트비트 나이 초과=행업/사망 워커.
        """
        import time as _t

        from sqlalchemy import text
        checks: dict[str, str] = {}
        ok = True
        try:
            await session.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as e:  # noqa: BLE001
            checks["db"] = f"fail: {str(e)[:80]}"
            ok = False
        # 정기업무 stale 신호(R11) — FAILED 최근·정체 DUE·고아 RUNNING·엔진 stale이면 degraded
        try:
            from erp.app.recurring_service import RecurringRunner
            sig = await RecurringRunner().health_signal(session)
            if sig["ok"]:
                checks["recurring"] = "ok"
            else:
                detail = {k: v for k, v in sig.items() if k != "ok" and v}
                checks["recurring"] = f"stale: {detail}"[:200]
                ok = False
        except Exception as e:  # noqa: BLE001 — 신호 산출 실패는 DB 문제(위 db 체크가 잡음)
            checks["recurring"] = f"fail: {str(e)[:80]}"
            ok = False
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(get_settings().redis_url)
            await r.ping()
            checks["redis"] = "ok"
            hb = await r.get("erp:heartbeat:worker-batch")
            if hb is None:
                checks["worker_batch"] = "stale (no heartbeat)"
                ok = False
            else:
                age = int(_t.time()) - int(hb)
                checks["worker_batch"] = f"ok ({age}s)" if age < 180 else f"stale ({age}s)"
                if age >= 180:
                    ok = False
            await r.aclose()
        except Exception as e:  # noqa: BLE001
            checks["redis"] = f"fail: {str(e)[:80]}"
            ok = False
        return JSONResponse({"status": "ok" if ok else "degraded", "checks": checks},
                            status_code=200 if ok else 503)

    @app.get("/api/metrics")
    async def metrics(request: Request, session: AsyncSession = Depends(get_session)):
        """Prometheus 메트릭(P2-3) — 요청 카운터·지연 + 헬스 게이지. 내부망 스크레이프는 무인증.

        의존성 없이 텍스트 포맷 직접 생성(requirements.lock 규율 회피, v1.85). 게이지는 스크레이프
        시점에 산출하며 실패해도 스크레이프를 죽이지 않도록 개별 try. 인프로세스 카운터라 재시작 리셋.
        ⚠ 종전엔 nginx가 내부망으로 제한한다는 전제였는데, 서버 이전(Cloudflare→Caddy) 후 그 제한이 없어
        공개 URL에서 경로별 요청 수·워커 상태가 그대로 보였다(2026-10-09 시나리오 S-F·prod 실측 200).
        프록시를 거친 요청(전달 헤더 존재)은 METRICS_TOKEN Bearer가 맞을 때만 응답한다 — 미설정이면 404.
        """
        import hmac as _hmac
        import time as _t

        if any(request.headers.get(h) for h in ("x-forwarded-for", "forwarded", "cf-connecting-ip", "x-real-ip")):
            want = get_settings().metrics_token
            got = request.headers.get("authorization", "")
            if not want or not _hmac.compare_digest(got.encode(), f"Bearer {want}".encode()):
                return PlainTextResponse("Not Found", status_code=404)

        from sqlalchemy import text

        from erp.app import metrics as _metrics
        lines: list[str] = []
        lines += ["# HELP erp_up 프로세스 가동", "# TYPE erp_up gauge", "erp_up 1"]
        lines += ["# HELP erp_build_info 빌드 정보", "# TYPE erp_build_info gauge",
                  f'erp_build_info{{env="{get_settings().env}"}} 1']

        db_up = 0
        try:
            await session.execute(text("SELECT 1"))
            db_up = 1
        except Exception:  # noqa: BLE001
            db_up = 0
        lines += ["# HELP erp_db_up DB 연결 가용", "# TYPE erp_db_up gauge", f"erp_db_up {db_up}"]

        redis_up = 0
        hb_age = -1
        try:
            import redis.asyncio as aioredis
            r = aioredis.from_url(get_settings().redis_url)
            await r.ping()
            redis_up = 1
            hb = await r.get("erp:heartbeat:worker-batch")
            if hb is not None:
                hb_age = int(_t.time()) - int(hb)
            await r.aclose()
        except Exception:  # noqa: BLE001
            redis_up = 0
        lines += ["# HELP erp_redis_up Redis 연결 가용", "# TYPE erp_redis_up gauge",
                  f"erp_redis_up {redis_up}"]
        lines += ["# HELP erp_worker_heartbeat_age_seconds worker-batch 하트비트 나이(-1=없음)",
                  "# TYPE erp_worker_heartbeat_age_seconds gauge",
                  f"erp_worker_heartbeat_age_seconds {hb_age}"]

        lines += _metrics.render_requests()
        return PlainTextResponse("\n".join(lines) + "\n",
                                 media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request, access_token: str | None = Cookie(default=None),
                      his_sso: str | None = Cookie(default=None),
                      session: AsyncSession = Depends(get_session)):
        """SSO 랜딩 — 프록시 루트(/erp/) 진입점. 서버 렌더(CSP 준수, 인라인 스크립트 없음).

        우선순위: ① ERP 세션(access_token) 있으면 사용자 표시 → ② his_sso(HIS 동일호스트
        핸드오프 쿠키) 있으면 검증·ERP 세션 발급·his_sso 삭제 후 재방문(1회 소비) → ③ 안내.
        """
        import logging

        from erp.common.router import exchange_his_sso, external_base
        from erp.common.security import decode_access_token
        from erp.common.sso_service import SsoReplayError

        log = logging.getLogger("erp.sso")

        def _redirect(path: str) -> RedirectResponse:
            """이 응답은 **절대 캐시되면 안 된다.**

            같은 URL(`/erp/`)이 쿠키 유무에 따라 대시보드로도, 로그인으로도 간다. 캐시되면
            브라우저가 **오리진에 묻지 않고** 저장된 리다이렉트를 재생해, HIS 핸드오프 쿠키가
            소비될 기회 자체가 사라진다(2026-09-09: 클릭해도 서버에 랜딩 요청이 오지 않고
            로그인 화면만 뜨는 증상 — 서버 로그에 요청조차 없어 원인 추적이 어려웠다).
            """
            resp = RedirectResponse(url=f"{external_base(request)}{path}", status_code=302)
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            resp.headers["Pragma"] = "no-cache"
            return resp

        # 기존 ERP 세션의 유효성(사용자 id) — 판단은 아래에서 핸드오프 쿠키 유무에 따라 갈린다.
        session_uid: str | None = None
        if access_token:
            try:
                session_uid = str(decode_access_token(access_token).get("sub"))
            except Exception as exc:  # noqa: BLE001 — 만료/위조면 세션 없음으로 취급
                log.info("랜딩: ERP 세션 쿠키 무효",
                         extra={"ctx": {"branch": "session_invalid",
                                        "reason": type(exc).__name__}})

        # ① 핸드오프 쿠키가 왔으면 **기존 세션보다 먼저** 소비한다.
        #    종전엔 세션을 먼저 봐서, HIS에서 다른 사용자로 바꿔 로그인해도 ERP는 이전 사용자로
        #    남았다(공용 PC) — 그리고 핸드오프 실패도 세션이 가렸다(v1.287.3 실측). his_sso는
        #    HIS 버튼을 방금 누른 사람의 명시적 의사이므로 그쪽이 정본이다.
        if his_sso:
            try:
                resp = await exchange_his_sso(session, his_sso, request)
                resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
                # 성공도 남긴다 — 성공이 무로그면 "기존 세션으로 들어간 것"과 구분되지 않는다
                # (HIS 회신 §6-3). 기존 세션을 덮어썼는지도 남긴다(사용자 교체 추적).
                log.info("랜딩: HIS 핸드오프 성공 — 세션 발급",
                         extra={"ctx": {"branch": "handoff_ok",
                                        "replaced_session": session_uid is not None}})
                return resp
            except SsoReplayError as exc:
                # 같은 토큰을 직전 요청이 이미 소비(프리페치·이중요청). 그 요청이 **이 세션의 사용자**에게
                # 세션을 발급했을 때만 진행한다(G3) — 아니면 남아 있던 이전 사용자 세션일 수 있다.
                same = session_uid is not None and exc.consumed_by == session_uid
                log.info("랜딩: 핸드오프 토큰 재사용(직전 요청이 소비)",
                         extra={"ctx": {"branch": "handoff_replay", "same_user": same,
                                        "consumed": exc.consumed_by is not None}})
                if same:
                    return _redirect("/dashboard")
                if exc.consumed_by is not None and request.query_params.get("replay") != "1":
                    # 동시 이중 요청(프리페치·더블클릭): 첫 요청이 세션을 발급했고 그 응답이 아직 도착
                    # 전일 수 있다. 여기서 쿠키를 지우면 늦게 온 이 응답이 방금 심은 세션을 지운다(H3).
                    # **한 번만** 랜딩을 다시 탄다 — 첫 응답이 왔으면 his_sso가 지워져 세션 분기로,
                    # 안 왔으면(첫 요청 실패·중단) replay=1로 다시 여기 와서 아래에서 쿠키를 지운다(I3).
                    return _redirect("/?replay=1")
                # 아무도 세션을 받지 못했다(첫 요청 실패) — 남은 이전 세션으로 들여보내지 않는다
                resp = _redirect("/login")
                _clear_session_cookies(resp, request)
                return resp
            except Exception as exc:  # noqa: BLE001 — 만료/위조 시 로그인으로 폴백
                # 🔴 사유를 반드시 남긴다(토큰 값은 남기지 않는다 — 사유와 예외형만).
                log.warning("HIS SSO 핸드오프 쿠키 소비 실패 — 로그인으로 폴백",
                            extra={"ctx": {"branch": "handoff_failed",
                                           "error": type(exc).__name__,
                                           "detail": str(exc)[:120],
                                           "had_session": session_uid is not None}})
                # 핸드오프가 실패했는데 기존 세션으로 들여보내면, 그 세션이 **방금 HIS에서 로그인한
                # 사람의 것이라는 보장이 없다**. 액세스 토큰이 만료돼 session_uid가 없어도 이전
                # 사용자의 refresh 쿠키가 남아 /dashboard에서 재발급되므로 **항상** 지운다(G2).
                resp = _redirect("/login")
                _clear_session_cookies(resp, request)
                return resp
        elif session_uid is not None:
            # ② 핸드오프 없이 기존 세션 → 대시보드 직행
            log.info("랜딩: 기존 ERP 세션 — 대시보드", extra={"ctx": {"branch": "session"}})
            return _redirect("/dashboard")
        else:
            log.info("랜딩: his_sso 쿠키 없음 — 로그인으로 폴백(HIS 발급/전송 확인 필요)",
                     extra={"ctx": {"branch": "no_cookie"}})

        # ③ 미인증 → 폴리시 웹 로그인(PIN+생체/HIS SSO)으로 유도
        return _redirect("/login")

    return app


app = create_app()
