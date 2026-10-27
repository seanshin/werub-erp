# 설계 — 감사·이상탐지 대시보드 UX/UI (`/finance-audit`)

> **작성일**: 2026-06-23 | **상태**: ✅ 기획 확정(구현 착수) | **선행**: [ADR D-7 분석도구](analysis-tooling-decision.md)
> **결정**: 독립 화면 `/finance-audit` · 기간 범위+프리셋(기본 당해연도 YTD) · 검토완료 워크플로 포함
> **권한**: `analytics.reports/read` (DIRECTOR·ADMIN, `_FIN`)

## 1. 핵심 원칙 (감사 윤리)

| 원칙 | UI 반영 |
|---|---|
| **후보 ≠ 판정** | "검토 후보"·"이상 신호" 표기. "부정/사기" 금지. red 남발 X(주의=amber 위주) |
| **설명가능성** | 모든 플래그에 *왜*(사유 칩, Benford 기대 vs 실측) |
| **실행가능성** | 후보 → 실제 전표로 1클릭 이동(`/finance-voucher-status`) |
| **통계적 정직** | 표본수·`<300 신뢰도 낮음`·Benford 자릿수 분산 캐비엇 노출 |

## 2. 레이아웃

```
[감사·이상탐지]  기간 [2026-01-01 ~ 06-23 ▾] [당월][당분기][당해] [↻]
전기완료(POSTED) 전표 분석 · 결과는 검토 후보이며 자동 판정이 아닙니다

요약 밴드(4 KPI): ①Benford 부합  ②중복전표 후보  ③비정상 분개  ④미검토 후보 합계
┌ ①Benford ─────────────┐ ┌ ②중복전표·중복지급 ──────┐
│ 막대(자리1~9 기대vs실측) │ │ 테이블(계정·금액·건수·거래처)│
│ χ²·MAD·verdict·▸상세    │ │ ▸클러스터 상세            │
└────────────────────────┘ └──────────────────────────┘
┌ ③비정상 분개(수동·입력시각 기준) [전체/주말/심야/라운드/한도직전] [미검토만☑] ┐
│ 전표·입력일시·금액·사유칩·적요·검토상태·[검토완료]·▸                          │
└──────────────────────────────────────────────────────────────────────────┘
```

## 3. 컴포넌트

### 3.1 요약 밴드 (`/audit/summary`)
| 카드 | 주값 | 톤 |
|---|---|---|
| Benford 부합 | `verdict` 배지 | 근접부합=green·허용가능=blue·한계=amber·부적합=red |
| 중복전표 후보 | `cluster_count`건 / `duplicate_amount` | 0=gray, 1+=amber |
| 비정상 분개 | `flagged_count`건 / by_reason 분해 | 0=gray, 1+=amber |
| **미검토 후보 합계** | `open_count`(검토완료 제외) | 항상 중립(slate) — 잔여 실행량 강조 |

> 0~100 "위험 스코어"는 만들지 않음 — 이질 신호의 단일 합산은 거짓 정밀도·오탐 과장.

### 3.2 Benford 패널 (`/audit/benford?side=`)
자리 1~9 기대(연회색)·실측(파랑) 병치 막대, 편차 큰 자리 강조. `χ²(임계 15.5)`·`MAD`·`verdict` 배지. side 토글(차변 기본). `note`·자릿수분산 캐비엇. ▸ 9자리 상세 모달.

### 3.3 중복 패널 (`/audit/duplicates?window_days=`)
테이블(계정·금액·건수·기간·거래처), 금액 내림차순. 빈상태=green("중복 후보 없음"). ▸ 클러스터 전표 목록 모달 → "전표 열기".

### 3.4 비정상 패널 (`/audit/abnormal?threshold=`)
사유 칩 필터(전체/주말/심야/라운드넘버/한도직전, 클라이언트). 사유 칩 색: 주말=purple·심야=blue·라운드=amber·한도직전=red. 헤더 "수동 입력·입력시각(KST) 기준". ▸ 전표 상세 모달.

## 4. 검토완료 워크플로

### 식별자(finding_key)
| 검사 | finding_key | 재계산 |
|---|---|---|
| 비정상 분개 | `voucher:<id>` | 전표 ID 안정 → 상태 유지 |
| 중복 클러스터 | `dup:<sha1(정렬 voucher_id들)>` | 멤버 변동 시 새 후보 재부상 |
| Benford | 없음(분포 신호) | 워크플로 제외, 정보용 |

### 모델 `finance.audit_reviews`
`finding_type(ABNORMAL|DUPLICATE)·finding_key·status(OPEN|REVIEWED)·disposition(NORMAL|ACTION)·note·reviewed_by·reviewed_at·created_at`, UNIQUE(finding_type, finding_key).

### 상태·색상 (중립)
- 미검토(OPEN) — amber "미검토"
- 검토완료·정상(NORMAL) — green "정상확인"(오탐·정상분개)
- 검토완료·조치(ACTION) — slate "조치필요" (red 아님 — 단정 회피)

### UI
요약밴드 ④ = `open_count`(검토완료 제외). 각 행 `[검토완료]` 팝오버(정상/조치 + 메모) → `POST /audit/reviews`. "미검토만 보기" 토글(기본 ON). 검토완료 행은 회색·처리자/일시 툴팁.

### API
```
POST   /api/v1/analytics/audit/reviews   {finding_type, finding_key, disposition, note?}
DELETE /api/v1/analytics/audit/reviews/{id}   (검토 취소 → 미검토)
GET 응답(summary/duplicates/abnormal)에 review:{id,status,disposition,...}|null 부착
```
검토는 기록 행위 → `reviewed_by`=현재 사용자. 권한 `analytics.reports/read`.

## 5. 상태·반응형·접근성
로딩=Spinner("분석 중…"), 빈결과=green 긍정, 저표본=`note`+"참고용" 칩, 에러=MessageBanner. 그리드 lg 2열→모바일 1열·테이블 가로스크롤. 색상 단독의존 금지(칩 텍스트 동반)·모달 포커스트랩/ESC(기존 Modal).

## 6. 구현 단위
```
백엔드: finance/models.py(AuditReview) · alembic 0109_audit_reviews
        analytics/audit_service.py(finding_key+review JOIN) · audit_review_service.py
        analytics/router.py(reviews POST/DELETE + review 부착) · tests
프론트: app/(app)/finance-audit/page.tsx + Summary/Benford/Duplicate/Abnormal 패널·모달
        components/ReviewControl.tsx · charts.tsx(GroupedBar) · menu_service.py 등록
```
