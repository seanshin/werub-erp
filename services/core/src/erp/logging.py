"""구조화 JSON 로그 + request_id 전파 — design §10, NFR 관측성."""
import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "request_id": request_id_var.get(),
            "module": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            entry["exc_type"] = str(record.exc_info[0].__name__)  # 메시지만 — 스택은 별도 보관
        extra = getattr(record, "ctx", None)
        if extra:
            entry.update(extra)
        return json.dumps(entry, ensure_ascii=False)


# `code`는 넣지 않는다 — 약품·수가 코드 같은 업무 쿼리까지 가려 관측성을 잃는다(G13).
# 2026-10-09 시나리오 S-F: 환자 식별 쿼리(chart_no·chartNo·이름·전화·생년월일)도 접근 로그에 원문으로
# 남았다(Phase0 "로그 chart_no 금지" 위반 — /claims/calc/from-chart·cases-export·HIS 폴링·/patients 검색).
# 자유 검색어(q·keyword·search)도 — 전화번호·이름으로 검색하면 그대로 남는다(2회차).
_SECRET_QS = re.compile(
    r"([?&](?:token|access_token|refresh_token|chart_?no|chartNo|patient_?name|name|phone|"
    r"birth_?date|birthDate|rrn|email|q|keyword|search)=)[^&\s\"]+", re.I)


def redact_query_secrets(text: str) -> str:
    """URL 쿼리의 토큰·환자 식별 값을 가린다 — `?token=eyJ…` → `?token=***`, `?chart_no=…` → `?chart_no=***`."""
    return _SECRET_QS.sub(r"\1***", text)


class AccessLogRedactFilter(logging.Filter):
    """uvicorn 접근 로그에서 쿼리 토큰 제거.

    token 방식 SSO는 HIS 메인 액세스 토큰(수 시간 유효)을 `GET /auth/sso/his?token=` 으로 받는다.
    uvicorn은 요청 줄 전체를 남기므로 토큰이 그대로 로그에 쌓였다 — 로그를 읽을 수 있는 사람은
    누구나 그 토큰으로 HIS에 로그인할 수 있다. 레코드 args(경로 포함)를 기록 전에 고쳐 쓴다.
    """
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(redact_query_secrets(a) if isinstance(a, str) else a
                                for a in record.args)
        elif isinstance(record.msg, str):
            record.msg = redact_query_secrets(record.msg)
        return True


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactFilter) for f in access.filters):
        access.addFilter(AccessLogRedactFilter())
