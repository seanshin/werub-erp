"""경량 Prometheus 메트릭(P2-3) — 외부 의존성 없이 텍스트 포맷 노출.

요청 카운터·지연(라우트 템플릿 라벨로 카디널리티 제한) + 스크레이프 시 헬스 게이지(DB/Redis/워커).
인프로세스 집계라 재시작 시 리셋(Prometheus counter reset 처리). 단일 컨테이너 전제.
"""
import threading

_lock = threading.Lock()
_req_count: dict[tuple, int] = {}        # (method, path, status) -> count
_req_dur_sum: dict[tuple, float] = {}    # (method, path) -> 누적 초
_req_dur_cnt: dict[tuple, int] = {}      # (method, path) -> 건수


def record(method: str, path: str, status: int, duration: float) -> None:
    key = (method, path, str(status))
    dkey = (method, path)
    with _lock:
        _req_count[key] = _req_count.get(key, 0) + 1
        _req_dur_sum[dkey] = _req_dur_sum.get(dkey, 0.0) + duration
        _req_dur_cnt[dkey] = _req_dur_cnt.get(dkey, 0) + 1


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render_requests() -> list[str]:
    lines = ["# HELP erp_http_requests_total HTTP 요청 수",
             "# TYPE erp_http_requests_total counter"]
    with _lock:
        for (m, p, s), c in sorted(_req_count.items()):
            lines.append(
                f'erp_http_requests_total{{method="{_esc(m)}",path="{_esc(p)}",status="{_esc(s)}"}} {c}')
        lines += ["# HELP erp_http_request_duration_seconds 요청 처리시간",
                  "# TYPE erp_http_request_duration_seconds summary"]
        for (m, p), total in sorted(_req_dur_sum.items()):
            cnt = _req_dur_cnt.get((m, p), 0)
            lines.append(
                f'erp_http_request_duration_seconds_sum{{method="{_esc(m)}",path="{_esc(p)}"}} {total:.6f}')
            lines.append(
                f'erp_http_request_duration_seconds_count{{method="{_esc(m)}",path="{_esc(p)}"}} {cnt}')
    return lines
