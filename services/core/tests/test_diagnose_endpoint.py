"""ai-diagnose 엔드포인트 — gather·예외 필터·부분실패·전체실패·타임아웃·클라생성 실패 경로.

순수 집계는 test_diagnose_majority.py가 커버. 여기서는 weve(WeveRagClient)를 monkeypatch해
동시 호출 조립부(asyncio.gather/wait_for, 예외 흡수)를 직접 검증한다.
"""
import threading
import time
from types import SimpleNamespace

import pytest

import erp.config
import erp.integrations.weve_rag as weve_mod
from erp.common.ratelimit import SlidingWindowLimiter
from erp.errors import RateLimitError
from erp.regulatory import router as reg

_USER = SimpleNamespace(user_id=1, role="ADMIN")


def _settings(samples=3, timeout=30.0):
    return SimpleNamespace(weve_ai_api_key="k", weve_ai_api_base="https://example.org/api/notice",
                           weve_diagnose_samples=samples, weve_diagnose_timeout=timeout)


def _ok(risk="LOW", cites=1):
    return {"has_evidence": cites > 0, "risk_level": risk, "risk_score": 0.5,
            "recommendation": "적정", "evidence_citations": [{"title": "고시"} for _ in range(cites)]}


def _fake_client(behaviors, *, init_raises=False, sleep=0.0):
    """behaviors: dict(반환) 또는 Exception(raise)의 리스트. 스레드 안전하게 순차 소비."""
    lock = threading.Lock()
    seq = list(behaviors)

    class _C:
        def __init__(self, *a, **k):
            if init_raises:
                raise ValueError("allowlist 위반")

        def diagnose(self, **kw):
            if sleep:
                time.sleep(sleep)
            with lock:
                b = seq.pop(0)
            if isinstance(b, BaseException):
                raise b
            return b

    return _C


def _patch(monkeypatch, client, settings=None):
    monkeypatch.setattr(erp.config, "get_settings", lambda: settings or _settings())
    monkeypatch.setattr(reg, "_require", lambda *a, **k: None)
    monkeypatch.setattr(reg, "_diag_limiter", SlidingWindowLimiter())   # 테스트 간 상태 격리

    async def _ntypes(session, code):
        return []
    monkeypatch.setattr(reg, "_notice_types_for_code", _ntypes)
    monkeypatch.setattr(weve_mod, "WeveRagClient", client)


async def _call():
    return await reg.ai_diagnose(reg.AiDiagnoseBody(code="AA001"), user=_USER, session=None)


async def test_partial_failure_marks_degraded(monkeypatch):
    # 3회 중 2회 예외 → 1건만 성공: degraded, 비가시화 없음
    _patch(monkeypatch, _fake_client([_ok("HIGH"), RuntimeError("x"), TimeoutError()]))
    out = await _call()
    assert out["available"] is True and out["degraded"] is True
    assert out["samples"] == 1 and out["attempted"] == 3 and out["risk_level"] == "HIGH"


async def test_all_failure_unavailable(monkeypatch):
    _patch(monkeypatch, _fake_client([RuntimeError(), RuntimeError(), RuntimeError()]))
    out = await _call()
    assert out["available"] is False and out["attempted"] == 3 and out["samples"] == 0


async def test_client_construction_failure(monkeypatch):
    _patch(monkeypatch, _fake_client([], init_raises=True))
    out = await _call()
    assert out["available"] is False and out["note"] == "근거검색 설정 오류" and out["attempted"] == 0


async def test_overall_timeout(monkeypatch):
    _patch(monkeypatch, _fake_client([_ok(), _ok(), _ok()], sleep=0.3), settings=_settings(timeout=0.01))
    out = await _call()
    assert out["available"] is False and out["note"] == "근거검색 응답 지연" and out["samples"] == 0


async def test_numeric_chart_no_blocked_before_external_send(session, monkeypatch):
    """심층 재검토 — 숫자전용 차트번호가 실데이터 사전대조로 외부 AI 전송 차단(정규식 미포착분).

    과거 ai-diagnose는 scan()에 known_ 미주입 → 접두 없는 숫자 차트번호가 외부 LLM으로 유출됐다."""
    from datetime import date

    from erp.errors import ValidationError as _VErr
    from erp.patient.models import Patient
    session.add(Patient(chart_no="87654321", name="환자", birth_date=date(1990, 1, 1)))
    await session.flush()
    monkeypatch.setattr(reg, "_require", lambda *a, **k: None)
    monkeypatch.setattr(reg, "_diag_limiter", SlidingWindowLimiter())
    monkeypatch.setattr(erp.config, "get_settings", lambda: _settings())
    body = reg.AiDiagnoseBody(code="AA001", context="환자 87654321 소견 검토 요망")
    with pytest.raises(_VErr, match="개인식별정보"):
        await reg.ai_diagnose(body, user=_USER, session=session)


async def test_missing_weve_key_short_circuits(monkeypatch):
    _patch(monkeypatch, _fake_client([_ok()]), settings=_settings())
    monkeypatch.setattr(erp.config, "get_settings",
                        lambda: SimpleNamespace(weve_ai_api_key="", weve_ai_api_base="https://example.org"))
    out = await _call()
    assert out["available"] is False and "미연결" in out["note"]


async def test_rate_limit_blocks_after_max(monkeypatch):
    # 사용자별 호출 제한 — max 2/window 내 3번째 호출은 429(외부 호출 전 차단)
    s = SimpleNamespace(weve_ai_api_key="k", weve_ai_api_base="https://example.org/api/notice",
                        weve_diagnose_samples=1, weve_diagnose_timeout=30.0,
                        weve_diagnose_rate_max=2, weve_diagnose_rate_window=60.0)
    _patch(monkeypatch, _fake_client([_ok(), _ok(), _ok()]), settings=s)
    assert (await _call())["available"] is True
    assert (await _call())["available"] is True
    with pytest.raises(RateLimitError) as ei:
        await _call()
    assert ei.value.status_code == 429
