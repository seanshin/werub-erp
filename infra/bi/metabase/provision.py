#!/usr/bin/env python3
"""Metabase 셀프서비스 BI 프로비저닝 — 컬렉션·모델·Metrics·Segments (멱등).

기획서 bi-enhancements.plan.md §2(셀프서비스 BI). Metabase OSS v0.51에 reporting 마트 11종을
비개발자가 SQL 없이 탐색하도록:
- **컬렉션 3종**(경영 BI·재무운영·감사리스크)로 분류.
- **모델 11종**: 마트별 한글 컬럼 alias 네이티브 모델(type=model) — 질문 빌더에서 한글 컬럼 노출.
- **Metrics**: 평균 월수익·평균 이직률·총 미사용검진권(v0.51 first-class, MBQL 집계).
- **Segments**: 적자 진료과·보고부문 진료과·미검토 감사월(MBQL 필터).

멱등: 이름으로 기존 존재 확인 후 생략(재실행 안전). Metabase 메타DB는 git 밖이라 이 스크립트가 SoT.

실행(prod): docker cp 후 core/호스트에서
    MB_ADMIN_CRED_FILE=<자격파일> python3 provision.py   # 파일 첫/끝 토큰 = 관리자 계정/비번(600)
⚠ 2026-09-09 서버 이전 후 종전 기본 경로(~/hospital-erp/metabase-admin.txt)는 **존재하지 않는다**
(9/8 백업에도 없음). 자격 파일을 직접 만들어 MB_ADMIN_CRED_FILE 로 지정할 것.
DB id=2(ERP reporting) 가정. 신규 마트 추가 시 여기 MODELS/METRICS/SEGMENTS에 항목 추가.
"""
import json
import os
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:3012"
DB_ID = 2                      # ERP reporting datasource
CRED_PATH = os.environ.get(
    "MB_ADMIN_CRED_FILE",
    "/home/deploy/his-backup/weruby/hospital-erp/metabase-admin.txt",
)


def _req(method, path, body=None, session=None):
    r = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"X-Metabase-Session": session} if session else {})})
    try:
        with urllib.request.urlopen(r) as resp:
            data = resp.read()
            return json.loads(data) if data.strip() else None   # DELETE/PUT는 204(빈 본문)
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} {method} {path}: {e.read()[:200]!r}")
        raise


def login():
    toks = open(CRED_PATH).read().split()
    return _req("POST", "/api/session", {"username": toks[0], "password": toks[-1]})["id"]


# ── 컬렉션 ───────────────────────────────────────────────────────────────────
COLLECTIONS = [
    ("경영 BI", "경영진 KPI·진료과 손익·재무비율·HR — 톱라인 경영 지표"),
    ("재무·운영", "자금수지·채권채무·손익구성·검진권·재고·자금예측 — 재무·운영 상세"),
    ("감사·리스크", "Benford·중복전표·비정상분개 — 감사·이상탐지"),
]

# ── 모델(마트별 한글 컬럼) ───────────────────────────────────────────────────
# (이름, 컬렉션, SQL) — alias로 한글 컬럼 노출. type=model.
MODELS = [
    ("[모델] 경영 KPI 월간", "경영 BI",
     "SELECT yyyymm AS 월, revenue AS 수익, net_income AS 순이익, total_assets AS 자산총계, "
     "total_liabilities AS 부채총계, ar_balance AS 미수금, headcount AS 재직인원, "
     "labor_cost AS 월인건비, turnover_pct AS 이직률, patients AS 환자수, "
     "deduction_loss AS 삭감손실, current_ratio AS 유동비율, debt_ratio_pct AS 부채비율, "
     "operating_margin_pct AS 의료이익률, inventory_value AS 재고자산, equity AS 자본총계 "
     "FROM reporting.mart_exec_kpi_monthly ORDER BY yyyymm"),
    ("[모델] 진료과별 손익 월간", "경영 BI",
     "SELECT yyyymm AS 월, department AS 진료과, revenue AS 수익, profit AS 손익, "
     "assets AS 자산, reportable AS 보고부문 FROM reporting.mart_dept_pl_monthly ORDER BY yyyymm, revenue DESC"),
    ("[모델] 진료과별 이직 월간", "경영 BI",
     'SELECT yyyymm AS 월, department AS 진료과, active AS 재직, "left" AS 퇴직, rate AS 이직률 '
     "FROM reporting.mart_hr_turnover_monthly ORDER BY yyyymm, rate DESC"),
    ("[모델] 연간 재무비율", "경영 BI",
     "SELECT year AS 연도, current_ratio AS 유동비율, debt_ratio AS 부채비율, "
     "equity_ratio AS 자기자본비율, operating_margin AS 의료이익률, "
     "labor_cost_ratio AS 인건비율, material_cost_ratio AS 재료비율 "
     "FROM reporting.mart_ratios_annual ORDER BY year"),
    ("[모델] 자금수지 월간", "재무·운영",
     "SELECT yyyymm AS 월, inflow AS 유입, outflow AS 유출, net AS 순현금 "
     "FROM reporting.mart_cashflow_monthly ORDER BY yyyymm"),
    ("[모델] 채권·채무 Aging", "재무·운영",
     "SELECT yyyymm AS 월, kind AS 구분, bucket_current AS 현재, bucket_1_30 AS \"1-30일\", "
     'bucket_31_60 AS "31-60일", bucket_61_90 AS "61-90일", bucket_90plus AS "90일+", '
     "total AS 합계 FROM reporting.mart_aging_monthly ORDER BY yyyymm, kind"),
    ("[모델] 기능별 손익 월간", "재무·운영",
     "SELECT yyyymm AS 월, total_revenue AS 총수익, total_expense AS 총비용, net_income AS 순이익, "
     "cogs AS 매출원가, sga AS 판관비, gross_profit AS 매출총이익, operating_income AS 영업이익 "
     "FROM reporting.mart_income_function_monthly ORDER BY yyyymm"),
    ("[모델] 검진권 운영 월간", "재무·운영",
     "SELECT yyyymm AS 월, issued_amount AS 발급, redeemed_amount AS 사용, refunded_amount AS 환급, "
     "settled_amount AS 정산, unredeemed_krw AS 미사용잔액 FROM reporting.mart_voucher_monthly ORDER BY yyyymm"),
    ("[모델] 재고 스냅샷 월간", "재무·운영",
     "SELECT yyyymm AS 월, total_value AS 재고자산, shortage_count AS 안전재고미달, "
     "dead_stock_count AS 장기재고 FROM reporting.mart_inventory_monthly ORDER BY yyyymm"),
    ("[모델] 자금수지 예측", "재무·운영",
     "SELECT yyyymm AS 기준월, bucket_idx AS 순번, label AS 구간, inflow AS 수금예정, "
     "outflow AS 지급예정, net AS 순, balance AS 예상잔액 FROM reporting.mart_cash_forecast "
     "ORDER BY yyyymm DESC, bucket_idx"),
    ("[모델] 감사·이상탐지 월간", "감사·리스크",
     "SELECT yyyymm AS 월, benford_verdict AS Benford판정, benford_sample_size AS 표본, "
     "duplicate_clusters AS 중복군집, duplicate_open AS 미검토중복, abnormal_flagged AS 비정상적발, "
     "abnormal_open AS 미검토비정상, open_total AS 미검토합 "
     "FROM reporting.mart_audit_flags_monthly ORDER BY yyyymm"),
]

# ── Metrics (MBQL 집계) — (이름, 테이블, 집계함수, 필드, 설명) ─────────────────
METRICS = [
    ("월평균 수익", "mart_exec_kpi_monthly", "avg", "revenue", "reporting 마트 월별 수익의 평균"),
    ("월평균 순이익", "mart_exec_kpi_monthly", "avg", "net_income", "월별 순이익 평균"),
    ("평균 이직률", "mart_hr_turnover_monthly", "avg", "rate", "진료과·월 이직률 평균(%)"),
    ("총 미사용 검진권잔액", "mart_voucher_monthly", "sum", "unredeemed_krw", "미사용 검진권 발급잔액 합(선수금)"),
]

# ── 기존 큐레이션 질문 재배치 — (질문 이름, 대상 컬렉션) ─────────────────────
REORG = [
    ("월별 수익·순이익 추이 (샘플)", "경영 BI"),
    ("진료과별 이직률(최근월)", "경영 BI"),
    ("연간 재무비율 추세", "경영 BI"),
    ("자금수지 추세", "재무·운영"),
    ("손익 추세(수익·매출총이익·영업이익)", "재무·운영"),
    ("채권·채무 Aging(최근월)", "재무·운영"),
    ("검진권 추세", "재무·운영"),
    ("자금수지 예측 6개월 (최신 스냅샷)", "재무·운영"),
]

# ── Segments (MBQL 필터) — (이름, 테이블, 필드, 연산, 값, 설명) ────────────────
SEGMENTS = [
    ("적자 진료과", "mart_dept_pl_monthly", "profit", "<", 0, "손익 < 0 인 진료과·월"),
    ("보고부문 진료과", "mart_dept_pl_monthly", "reportable", "=", True, "K-IFRS 1108 보고부문 임계 충족"),
    ("미검토 감사 존재월", "mart_audit_flags_monthly", "open_total", ">", 0, "미검토 감사·이상탐지 > 0"),
]


def main():
    s = login()
    print("login ok")

    # 컬렉션
    existing_cols = {c["name"]: c["id"] for c in _req("GET", "/api/collection", session=s)}
    col_id = {}
    for name, desc in COLLECTIONS:
        if name in existing_cols:
            col_id[name] = existing_cols[name]
            print(f"collection 존재: {name} (#{existing_cols[name]})")
        else:
            c = _req("POST", "/api/collection", {"name": name, "description": desc}, s)
            col_id[name] = c["id"]
            print(f"collection 생성: {name} (#{c['id']})")

    # 테이블·필드 id 맵(메타데이터)
    meta = _req("GET", f"/api/database/{DB_ID}/metadata", session=s)
    tbl = {t["name"]: t for t in meta["tables"]}
    fid = {(t["name"], f["name"]): f["id"] for t in meta["tables"] for f in t["fields"]}

    # 모델(이름 존재 시 생략)
    cards = _req("GET", "/api/card", session=s)
    have = {c["name"] for c in cards}
    for name, coll, sql in MODELS:
        if name in have:
            print(f"model 존재: {name}")
            continue
        c = _req("POST", "/api/card", {
            "name": name, "type": "model", "collection_id": col_id[coll],
            "display": "table", "visualization_settings": {},
            "dataset_query": {"type": "native", "database": DB_ID, "native": {"query": sql}}}, s)
        print(f"model 생성: {name} (#{c['id']}) → {coll}")

    # Metrics(v0.51 type=metric·MBQL)
    have_metrics = {c["name"] for c in cards if c.get("type") == "metric"}
    for name, table, agg, field, desc in METRICS:
        if name in have_metrics:
            print(f"metric 존재: {name}")
            continue
        tid, f = tbl[table]["id"], fid[(table, field)]
        c = _req("POST", "/api/card", {
            "name": name, "type": "metric", "description": desc,
            "display": "scalar", "visualization_settings": {},
            "dataset_query": {"type": "query", "database": DB_ID,
                              "query": {"source-table": tid,
                                        "aggregation": [[agg, ["field", f, None]]]}}}, s)
        print(f"metric 생성: {name} (#{c['id']})")

    # 기존 큐레이션 질문 재배치(이미 대상 컬렉션이면 생략)
    card_by_name = {c["name"]: c for c in _req("GET", "/api/card", session=s)}
    for qname, coll in REORG:
        c = card_by_name.get(qname)
        if not c:
            print(f"질문 없음(재배치 생략): {qname}")
            continue
        if c.get("collection_id") == col_id[coll]:
            print(f"질문 배치됨: {qname} → {coll}")
            continue
        _req("PUT", f"/api/card/{c['id']}", {"collection_id": col_id[coll]}, s)
        print(f"질문 이동: {qname} → {coll}")

    # Segments(MBQL 필터)
    seg_have = {sg["name"] for sg in _req("GET", "/api/segment", session=s)}
    for name, table, field, op, val, desc in SEGMENTS:
        if name in seg_have:
            print(f"segment 존재: {name}")
            continue
        tid, f = tbl[table]["id"], fid[(table, field)]
        _req("POST", "/api/segment", {
            "name": name, "description": desc, "table_id": tid,
            "definition": {"source-table": tid, "filter": [op, ["field", f, None], val]}}, s)
        print(f"segment 생성: {name}")

    # Sample DB·예시 컬렉션 정리(아카이브) — 신규 설치의 데모 콘텐츠가 셀프서비스 탐색을 어지럽힘.
    # 삭제 대신 아카이브(가역). ERP reporting(id=2)·운영 컬렉션은 건드리지 않음.
    for db in _req("GET", "/api/database", session=s).get("data", []):
        if db.get("is_sample"):
            try:
                _req("DELETE", f"/api/database/{db['id']}", session=s)
                print(f"Sample DB 제거: {db['name']} (#{db['id']})")
            except Exception as exc:  # noqa: BLE001
                print(f"Sample DB 제거 실패(무시): {exc}")
    for c in _req("GET", "/api/collection", session=s):
        if c.get("name") == "Examples" and not c.get("archived") and not c.get("personal_owner_id"):
            _req("PUT", f"/api/collection/{c['id']}", {"archived": True}, s)
            print(f"예시 컬렉션 아카이브: Examples (#{c['id']})")

    print("provision 완료")


if __name__ == "__main__":
    sys.exit(main())
