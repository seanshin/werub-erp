"""웹 화면의 버전 표기 = 백엔드 릴리즈 버전 — 드리프트 차단.

웹 `APP_VERSION`이 "릴리스 시 여기만 갱신"이라는 주석과 달리 1.9.0에 멈춰 있어, 운영이 1.287.x인 동안
로그인 화면·사이드바가 `v1.9.0`을 보였다.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_web_app_version_matches_core():
    core = re.search(r'^version = "([^"]+)"', (ROOT / "services/core/pyproject.toml").read_text(),
                     re.M).group(1)
    web = re.search(r'APP_VERSION = "([^"]+)"',
                    (ROOT / "apps/web/src/lib/app-info.ts").read_text()).group(1)
    assert web == core, f"웹 APP_VERSION {web} ≠ core {core} — apps/web/src/lib/app-info.ts 갱신"
