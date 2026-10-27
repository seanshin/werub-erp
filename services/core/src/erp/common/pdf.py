"""PDF 렌더링 — WeasyPrint + 보안 url_fetcher (design §6.7).

⚠️ WeasyPrint는 CSS/HTML의 url()을 실제 fetch함 — 커스텀 fetcher로
   로컬 자산 화이트리스트(절대경로 prefix)만 허용, file://·http(s)://·data: 전부 차단 (LFI/SSRF).
Jinja2 autoescape=True 고정 — 사용자 입력 변수에 `| safe` 금지 (코드 리뷰 체크 항목).
"""
import logging
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

logger = logging.getLogger("erp.common.pdf")

ASSETS_DIR = (Path(__file__).parent / "pdf_assets").resolve()  # 번들 폰트·로고만
TEMPLATES_DIR = (Path(__file__).parent / "pdf_templates").resolve()

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(default=True, default_for_string=True),  # 고정
)


class BlockedUrlError(Exception):
    pass


def secure_url_fetcher(url: str):
    """§6.7 — 번들 자산 외 모든 fetch 차단 (예외 발생, 폴백 없음)."""
    if url.startswith("assets:"):
        target = (ASSETS_DIR / url.removeprefix("assets:")).resolve()
        if not str(target).startswith(str(ASSETS_DIR)):  # 경로 탈출 차단
            raise BlockedUrlError(f"asset 경로 탈출 차단: {url}")
        if not target.exists():
            raise BlockedUrlError(f"미존재 asset: {url}")
        return {"file_obj": open(target, "rb")}  # noqa: SIM115 — weasyprint가 close
    if url.startswith("data:image/"):
        # 인라인 **래스터** 이미지 base64(기관 로고 등)만 허용 — 네트워크·파일 접근 없음(LFI/SSRF 무관).
        # 재검토(심층방어): svg 등 비래스터·비이미지 data:는 차단(SVG 외부참조/스크립트 여지 원천 차단).
        import base64
        import re
        m = re.match(r"data:(image/(?:png|jpe?g|webp));base64,(.*)$", url, re.DOTALL)
        if m is None:
            raise BlockedUrlError("허용되지 않은 인라인 이미지(png/jpeg/webp base64만)")
        try:
            data = base64.b64decode(m.group(2), validate=True)
        except Exception as e:   # noqa: BLE001 — 잘못된 base64는 crash(500) 대신 차단으로 fail-closed
            raise BlockedUrlError("잘못된 인라인 이미지 base64") from e
        return {"string": data, "mime_type": m.group(1)}
    # file:// http:// https:// data:(비래스터) 등 전부 차단 — LFI/SSRF (design §6.7)
    logger.warning("PDF url fetch 차단", extra={"ctx": {"url": url[:120]}})
    raise BlockedUrlError(f"외부 리소스 차단: {url.split(':', 1)[0]}:")


def render_pdf(template_name: str, context: dict) -> bytes:
    """템플릿 → PDF. weasyprint는 지연 import (미설치 환경 graceful)."""
    from weasyprint import HTML  # 런타임 의존: pango (배포 이미지에 포함)

    html = _env.get_template(template_name).render(**context)
    return HTML(string=html, url_fetcher=secure_url_fetcher,
                base_url="assets:").write_pdf()
