"""MEDRLT 한정자(qualifier) 매핑 테이블 — ⚠️ 실물 샘플 검증 전 잠정값 (G1 운영 게이트).

공고 별표1(2021-55호)의 구조도는 세그먼트·필드 *의미*(명세서 일련번호, 접수번호,
심사 결정액, 조정사유 등)는 명시하나, RFF/MOA/FTX의 한정자 *코드값*은 실물 전문으로
확정해야 한다. 본 테이블은 의미 기반 잠정 코드 — **실물 2건 대조 후 이 파일만 보정**.

설계 원칙: 파서 코어(edifact.py)와 분리 — 코드값 보정이 파서 로직을 건드리지 않게.
"""

# RFF 한정자 → 의미 (구조도 라벨 기준)
RFF = {
    "ACE": "receipt_no",        # 접수번호 (SG1)
    "ABN": "claim_no",          # 청구번호
    "AFA": "stmt_serial",       # 명세서 일련번호 (SG3)
    "LI": "line_no",            # 줄번호 (SG4)
    "AGW": "adj_basis",         # 관련근거
}

# MOA 한정자 → 의미 (구조도 금액 라벨 기준)
MOA = {
    "79": "claimed_amt",        # 청구액 / 요양급여비용총액
    "52": "decided_amt",        # 심사 결정액
    "129": "adjusted_amt",      # 조정금액 (SG5)
    "25": "copay_amt",          # 본인일부부담금
}

# FTX 한정자 → 의미
FTX = {
    "ACB": "adj_reason",        # 조정사유 / 조정상세사유
    "AAO": "stmt_note",         # 명일련 비고사항
}

# CCI/특성 — 진료과·심사구분 등 (실물 확정 대상)
CCI_DEPT = "DEPT"

# DTM 한정자
DTM = {
    "137": "notified_on",       # 통보일자 (BGM 후 DTM)
    "194": "care_start_on",     # 요양개시일
}
