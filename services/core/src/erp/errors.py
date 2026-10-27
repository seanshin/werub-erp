"""공통 예외 체계 — design §4.2 에러 코드. 스택트레이스 미노출 원칙."""
from typing import Any


class ERPException(Exception):
    """모듈 공통 예외 — 전역 핸들러가 {error:{code,message,details}} 포맷으로 변환."""

    status_code = 400

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None,
                 status_code: int | None = None):
        self.code = code
        self.message = message
        self.details = details or {}
        if status_code is not None:
            self.status_code = status_code
        super().__init__(f"{code}: {message}")


# 인증/권한 (ERP-1xxx)
class AuthFailedError(ERPException):
    def __init__(self, message: str = "이메일 또는 비밀번호가 올바르지 않습니다"):
        super().__init__("ERP-1001", message, status_code=401)


class AccountLockedError(ERPException):
    def __init__(self, until: str):
        super().__init__("ERP-1002", "계정이 잠겼습니다", {"locked_until": until}, status_code=423)


class TokenError(ERPException):
    def __init__(self, message: str = "유효하지 않은 토큰입니다"):
        super().__init__("ERP-1003", message, status_code=401)


class ForbiddenError(ERPException):
    def __init__(self, message: str = "권한이 없습니다"):
        super().__init__("ERP-1004", message, status_code=403)


class LicenseFlagError(ERPException):
    def __init__(self):
        super().__init__("ERP-1005", "면허 갱신 필요 — 결재 권한이 제한되었습니다", status_code=403)


class RateLimitError(ERPException):
    def __init__(self, retry_after: int | None = None):
        super().__init__("ERP-1006", "요청이 너무 잦습니다 — 잠시 후 다시 시도하세요",
                         {"retry_after": retry_after} if retry_after else None, status_code=429)


# 검증 (ERP-2xxx)
class ValidationError(ERPException):
    def __init__(self, message: str, details: dict | None = None):
        super().__init__("ERP-2003", message, details, status_code=422)


# 비즈니스 (ERP-3xxx)
class FeeNotFoundError(ERPException):
    def __init__(self, code: str, at_date: str):
        super().__init__("ERP-3006", "해당 시점의 단가가 없습니다",
                         {"code": code, "at_date": at_date}, status_code=404)


# 연계 (ERP-4xxx)
class DispatchBlockedError(ERPException):
    def __init__(self, details: dict):
        super().__init__("ERP-4002", "민감정보 탐지 — AI 발신이 차단되었습니다 (관리자 검토 필요)",
                         details, status_code=409)
