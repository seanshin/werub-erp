"""FR-53 필터 게이트 — 우회 벡터 케이스 필수 (design §5.3)."""
from erp.regulatory.filter_gate import rrn_checksum_valid, scan

# 체크섬 유효한 테스트용 주민번호 (실존 아님 — 알고리즘 산출값)
VALID_RRN = "9012251234560"


def _make_valid_rrn() -> str:
    """체크섬이 맞는 13자리 생성."""
    base = "901225123456"
    weights = [2, 3, 4, 5, 6, 7, 8, 9, 2, 3, 4, 5]
    total = sum(int(d) * w for d, w in zip(base, weights))
    check = (11 - total % 11) % 10
    return base + str(check)


RRN = _make_valid_rrn()


def test_rrn_checksum():
    assert rrn_checksum_valid(RRN)
    assert not rrn_checksum_valid(RRN[:-1] + str((int(RRN[-1]) + 1) % 10))


def test_plain_rrn_detected():
    assert scan(f"환자 주민번호 {RRN[:6]}-{RRN[6:]}").blocked


def test_rrn_with_spaces_detected():
    """우회 벡터: 공백 삽입."""
    spaced = " ".join(RRN)
    assert scan(f"번호: {spaced}").blocked


def test_rrn_fullwidth_digits_detected():
    """우회 벡터: 전각 숫자."""
    fullwidth = RRN.translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    assert scan(f"번호 {fullwidth}").blocked


def test_rrn_zero_width_detected():
    """우회 벡터: zero-width 문자 삽입."""
    zw = "​".join(RRN)
    assert scan(zw).blocked


def test_rrn_special_dash_detected():
    """우회 벡터: 특수 대시 (en/em dash)."""
    assert scan(f"{RRN[:6]}–{RRN[6:]}").blocked  # en dash
    assert scan(f"{RRN[:6]}—{RRN[6:]}").blocked  # em dash


def test_invalid_checksum_not_blocked():
    """체크섬 무효 13자리는 오탐 억제 — 차단 안 함."""
    bad = RRN[:-1] + str((int(RRN[-1]) + 1) % 10)
    assert not scan(f"문서번호 {bad}").blocked


def test_phone_detected():
    assert scan("문의: 010-1234-5678").blocked


def test_chart_no_pattern_detected():
    assert scan("환자 C-10293 입원").blocked


def test_known_chart_no_exact_match():
    r = scan("대상자 ABC9988 외 1명", known_chart_nos={"ABC9988"})
    assert r.blocked and r.findings[0]["type"] == "CHART_NO_EXACT"


def test_known_chart_no_with_separators_detected():
    """심층 재검토 HIGH — 구분자 낀 차트번호도 사전 대조로 차단(외부 AI 유출 방지).

    과거 canon만 대조해 '1234-5678'/'1234 5678'이 우회(실제 차트 12345678 유출)했다."""
    for body in ("환자 1234-5678 소견", "환자 1234 5678 소견", "chart 1234·5678"):
        r = scan(body, known_chart_nos={"12345678"})
        assert r.blocked, body
        assert r.findings[0]["type"] == "CHART_NO_EXACT"
    # 사번도 동일
    assert scan("담당 EMP-2020-001", known_employee_nos={"EMP2020001"}).blocked


def test_clean_notice_passes():
    clean = "요양급여 적용기준 변경: 제1조 산정 기준은 2026-07-01부터 시행한다."
    assert not scan(clean).blocked
