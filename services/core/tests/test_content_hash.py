"""content_hash 정규화 7항 검증 — 연동 스펙 v1.0 (테스트 벡터 교환 대비)."""
from erp.regulatory.content_hash import content_hash, normalize_body


def test_bom_removed():
    assert normalize_body("﻿본문") == "본문\n"


def test_crlf_and_cr_to_lf():
    assert normalize_body("줄1\r\n줄2\r줄3") == "줄1\n줄2\n줄3\n"


def test_trailing_whitespace_per_line_removed():
    assert normalize_body("줄1  \t\n줄2 ") == "줄1\n줄2\n"


def test_document_end_single_newline():
    assert normalize_body("본문\n\n\n") == "본문\n"
    assert normalize_body("본문") == "본문\n"


def test_internal_spaces_preserved():
    # 6항: 표 서식 보호 — 행 내부 연속 공백 보존
    assert normalize_body("col1    col2") == "col1    col2\n"


def test_nfc_normalization():
    decomposed = "가̇"  # NFD 류 결합
    assert normalize_body(decomposed) == normalize_body(
        __import__("unicodedata").normalize("NFC", decomposed))


def test_same_content_same_hash_across_encodings():
    a = content_hash("심사기준 변경\r\n제1조  내용  \n")
    b = content_hash("﻿심사기준 변경\n제1조  내용\n\n")
    assert a == b


def test_hash_is_sha256_hex():
    h = content_hash("test")
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
