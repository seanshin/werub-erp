"""이메일 알림 발송 (FR-27) — SMTP. 미설정 시 no-op(가용성: 알림 실패가 업무 차단 금지).

격리망 on-prem 전제 — 내부 SMTP 릴레이 사용. smtp_host 미설정 시 발송하지 않고 로그만.
"""
import logging
from email.message import EmailMessage

from erp.common.crypto import mask_email
from erp.config import get_settings

logger = logging.getLogger("erp.common.email")


def send_email(to: str, subject: str, body: str) -> bool:
    """이메일 발송 — 성공 True. SMTP 미설정/실패 시 False(예외 전파 안 함)."""
    s = get_settings()
    if not s.smtp_host:
        logger.info("이메일 발송 생략(SMTP 미설정)", extra={"ctx": {"to": mask_email(to), "subject": subject}})
        return False
    msg = EmailMessage()
    msg["From"] = s.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        import smtplib

        with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=10) as smtp:
            smtp.starttls()
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(msg)
        logger.info("이메일 발송", extra={"ctx": {"to": mask_email(to), "subject": subject}})
        return True
    except Exception as exc:  # noqa: BLE001 — 알림 실패는 업무 비차단
        logger.warning("이메일 발송 실패", extra={"ctx": {"to": mask_email(to), "err": str(exc)[:120]}})
        return False
