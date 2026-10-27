# IFRS 정합 — 데이터 모델·계정·엔드포인트 통합 참조

> 각 IFRS 항목 구현에서 추가된 **테이블·계정과목·엔드포인트**를 한눈에 정리한 설명 자료.
> 항목별 상세(산식·가정·통제)는 methodology/policy/control 문서 참조.
>
> **작성일**: 2026-06-22 · 갱신: 단계 진행 시.

## 1. 신규 테이블 (마이그레이션)

| 테이블 | 마이그 | 용도(기준) | 핵심 컬럼 |
|---|---|---|---|
| `finance.allowance_rates` | 0087 | 대손 ECL 요율(1109) | kind·bucket·rate |
| `finance.tax_rate_settings` | 0088 | 법인세 유효세율(1012) | rate·valid_from |
| `scm.items`(+컬럼) | 0089 | 재고 저가법(1002) | nrv_unit·cost_method |
| `hr.actuarial_assumptions` | 0090 | 퇴직 보험수리 가정(1019) | discount_rate·salary_growth·retirement_age |
| `finance.disclosure_notes` | 0091 | 주석(1001) | period·note_key·payload |
| `finance.lease_contracts`(+컬럼) | 0095 | 리스 면제(1116) | exempt |
| `finance.sim_scenarios` | 0086 | (재무 시뮬) | user_id·drivers |

## 2. 신규 계정과목 (kind)

| 코드 | 명칭 | kind | 기준 | 비고 |
|---|---|---|---|---|
| 1190 | 대손충당금 | ASSET(차감) | 1109 | |
| 5690 | 대손상각비 | EXPENSE | 1109 | ※5660=외환차손과 구분 |
| 1180 | 이연법인세자산 | ASSET | 1012 | |
| 2260 | 이연법인세부채 | LIABILITY | 1012 | |
| 2230 | 미지급법인세 | LIABILITY | 1012 | |
| 5810 | 법인세비용 | EXPENSE | 1012 | |
| 1159 | 재고자산평가충당금 | ASSET(차감) | 1002 | |
| 5540 | 재고자산평가손실 | EXPENSE | 1002 | |
| 3410 | 보험수리적손익 | CAPITAL(OCI) | 1019 | |
| 4640 | 유형자산손상차손환입 | REVENUE | 1036 | ※4630=외환차익과 구분 |
| 5645 | 단기·소액리스료 | EXPENSE | 1116 | |

> **교훈**: 신규 계정코드는 prod 기존 계정과 충돌 가능(4630 외환차익·5660 외환차손) → 시드 전 충돌 확인 필수.
> ON CONFLICT DO NOTHING이 조용히 무시하므로, 충돌 시 빈 코드로 분리.

## 3. 신규 엔드포인트

| 엔드포인트 | 기준 | 권한 |
|---|---|---|
| `GET/POST /finance/allowance/preview·post` | 1109 | finance.periods |
| `GET/POST /finance/deferred-tax/preview·post` | 1012 | finance.periods |
| `GET/POST /inventory/nrv/preview·post`·`PATCH /inventory/items/{id}/nrv` | 1002 | finance.periods·scm.items |
| `GET/POST /hr/retirement/dbo/preview·post` | 1019 | hr.payroll |
| `GET /finance/statements/comprehensive-income` | 1001 | finance.statements |
| `GET/PUT /finance/disclosures` | 1001 | finance.statements |
| `POST /finance/assets/{id}/reverse-impairment` | 1036 | finance.assets |
| `POST /finance/leases/{id}/remeasure`·`POST /finance/leases`(exempt) | 1116 | finance.assets |

## 4. 결산 조정 전표 네임스페이스
- 측정 조정 전표는 `source_module="closing-ifrs"`(대손·이연법인세·재고저가·퇴직재측정), 자산/리스는 `"asset"`/`"lease"`.
- 모두 멱등(source_ref)·역분개 가능. 결산 조정순서: 손상→대손→재고→외화→퇴직재측정→이연법인세→이익잉여금.
- **closing-ifrs 조정은 당기손익/현금흐름에 의도적으로 포함**(대손상각비·외화환산손익·평가손실 등은 당기비용/수익). exclude_closing 필터(`source_module!='closing'`)는 연말 P&L→이익잉여금 마감만 제외(IFRS 조정 유지).
