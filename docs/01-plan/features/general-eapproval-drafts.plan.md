# 일반 전자결재(기안문서) 기획 v0.1 — 자유형식 품의·공람 (내부운영 갭 감사 Tier1)

> ⚠ **선행 기반**: `approval-line-management.plan.md`(결재선 요청·관리) — 기안문서는 그 결재선을 **선택**해
> 상신한다. 결재선을 "요청·생성·관리"하는 기능이 먼저 확정·구현돼야 한다(사용자 지침 2026-07-21). 아래
> §1의 "ad-hoc 자유 지정"은 선행 기획의 R-2(ad-hoc 허용 범위) 결정에 따라 "템플릿 선택"으로 대체/병행될 수 있다.
>
> **문제**: N단계 결재엔진·불변 원장·통합결재함이 구축돼 있으나 **구조화 레코드 타입에만 바인딩**
> (PO·위수탁/의료 계약·전표·ITAM 폐기·경비). 전 직원이 상시 쓰는 **자유형식 기안**(지출품의 외 일반 품의·
> 업무협조·규정 개정·공람)이 없어 내부통제 문서화가 시스템 밖(수기·메신저)에서 이뤄짐.

## 0. 경계 결정 (선행 — 사용자 확인 필요)

병원에는 결재 표면이 둘 있다:

| 표면 | 현황 | 용도 |
|---|---|---|
| **HIS 그룹웨어(W.Sign e결재)** | 휴가 상신 연동 존재(`hr/eapproval_service`·플래그+키 시 활성)·HIS 자체 eapproval(제증명 발급 등) | 임상·원무 행정 문서 |
| **ERP 결재엔진** | PO·계약·전표·경비 등 **ERP 레코드에 결합된** 결재·불변 원장(HMAC 해시체인)·결재함 | ERP 업무 승인 |

**본 기획의 위치**: ERP 자체 **일반 기안문서**는 "ERP 업무 문맥의 자유형식 승인"(예: 예산 외 지출 품의,
내규/마스터 변경 품의, 부서 간 업무협조 — **결재 증적이 ERP 감사·불변 원장에 남아야 하는 문서**)을
담당한다. 전사 공지·인사발령·임상 문서 등 그룹웨어 성격 문서는 HIS W.Sign 영역으로 남긴다(중복 회피).
→ **결정 A**: 이 경계로 진행할지, 아니면 일반 기안 전체를 HIS W.Sign에 위임하고 ERP는 미구축할지.

## 1. 스코프 (v1)

- **기안문서(GeneralDraft)**: 제목·본문(텍스트)·분류(category)·첨부(범용 Attachment 재사용)·기안 부서.
- **자유 결재선(ad-hoc)**: 기안자가 결재자(사용자)를 순서대로 직접 지정(1~8단계). 기존 룰 기반과 별개 —
  **엔진 확장 `build_adhoc(steps=[user_id…])`**(ApprovalStep `approver_type=USER` 재사용).
- **결재 진행**: 기존 `act()` 재사용 → **불변 원장(BUILD/APPROVE/REJECT) 자동 기록**·통합결재함
  (`/approvals/worklist`) 자동 노출·CEO override 재사용.
- **공람(CC)**: 결재 완료 시 지정된 공람자에게 알림(Notification)+열람 기록(읽음 시각).
- **문서번호 채번**: `기안-{YYYY}-{seq}`(연도별 순번·기관 표기 관례).
- **상태머신**: `DRAFT → PENDING → APPROVED | REJECTED`, DRAFT/REJECTED에서 `CANCELED`(기안자 회수).
  REJECTED 후 재기안 = 새 문서(문서번호 신규·이력 보존).

## 2. 비범위 (v1 제외 — 명시)

- 본문 리치텍스트/템플릿·기안 양식 빌더 (v1=플레인 텍스트+첨부).
- 대결(위임 결재)·전결 규정 엔진 (기존 override로 CEO 전결만).
- 병렬 합의(동시 결재)·후열 — v1은 순차 단계만(엔진 현행).
- HIS W.Sign으로의 발신/수신 브리지 (결정 A에 따라 후속).
- 결재 완료 문서의 PDF 고정본 생성(후속 — 현재 불변 원장이 변조 증거 담당).

## 3. 데이터 모델 (마이그 0211)

```
common.general_drafts
  id BIGSERIAL PK
  doc_no        VARCHAR(20) UNIQUE      -- 기안-2026-0001 (연도별 순번)
  title         VARCHAR(200)
  body          TEXT
  category      VARCHAR(24)             -- GENERAL|BUDGET|POLICY|COOPERATION (분류·통계 축)
  department_id BIGINT                  -- 기안 부서(행 스코프·부서장 결재 해소)
  status        VARCHAR(10) CHECK (DRAFT|PENDING|APPROVED|REJECTED|CANCELED)
  created_by    BIGINT                  -- 기안자 user id
  decided_at    TIMESTAMPTZ
  created_at    TIMESTAMPTZ

common.draft_viewers                    -- 공람
  id BIGSERIAL PK
  draft_id      BIGINT FK → general_drafts
  user_id       BIGINT
  notified_at   TIMESTAMPTZ
  read_at       TIMESTAMPTZ
  UNIQUE(draft_id, user_id)
```
결재선 자체는 기존 `common.approval_instances/steps`(doc_type="GENERAL_DRAFT") 재사용 — 신규 테이블 없음.

## 4. 엔진 확장 — `build_adhoc`

현행 `build()`는 ApprovalRule 매칭 필수(룰 없으면 None). 일반 기안은 문서마다 결재선이 달라 룰로 못 담음.

```python
async def build_adhoc(session, *, doc_type, doc_id, approver_ids: list[int],
                      created_by, department_id=None, amount=0) -> ApprovalInstance
```
- steps = `[{USER, user_id}, …]` 순서대로. 검증: 1~8단계·중복 결재자 불가·**기안자 본인 포함 불가(SoD)**·
  실재 사용자(존재+활성).
- 인스턴스/단계 생성·불변 원장 BUILD 기록 — `build()`와 동일 경로(원장 규약 불변).
- 기존 `act()`/`pending_for()`/`get()` 무수정 재사용(USER 타입 이미 지원).

## 5. 권한 (RBAC)

- 신규 리소스 `common.drafts`:
  - `read`: 전 역할(WORKSTATION 제외) — SELF(본인 기안+본인 공람+본인 결재 관여) / MANAGER DEPT / ADMIN·DIRECTOR ALL.
  - `write`(기안·회수): 전 역할 SELF (경비 finance.expenses와 동일 어휘).
- 결재 행위 = 기존 `common.approvals write` + 엔진 적격성(`_eligible` USER 매칭) — 지정 결재자만 승인 가능.
- 행 스코프: 조회 시 본인 관여(기안자·결재자·공람자) 또는 DEPT/ALL 명시 WHERE(IDOR 방지·경비 패턴).
- 첨부: `_ENTITY_SCOPE["draft"] = "common.drafts"` + 행 가드(관여자만).

## 6. EP 설계 (v1)

| EP | 동작 | 게이트 |
|---|---|---|
| `POST /drafts` | 기안(DRAFT·결재선 후보 저장 아님 — 본문만) | drafts write |
| `GET /drafts` / `GET /drafts/{id}` | 목록(스코프 합성)·상세(+결재선·공람 상태) | drafts read+행 |
| `PUT /drafts/{id}` | DRAFT만 수정 | 기안자 |
| `POST /drafts/{id}/submit` | 상신 — `approver_ids[]`·`viewer_ids[]` 지정 → build_adhoc | 기안자 |
| `POST /drafts/{id}/approve|reject` | 결재(act 재사용·완주 시 APPROVED+공람 알림) | approvals write+적격 |
| `POST /drafts/{id}/cancel` | 회수 — DRAFT, 또는 PENDING&아무도 미결재 시 | 기안자 |
| `POST /drafts/{id}/read` | 공람 열람 확인 | 공람자 본인 |

approval_router `_DOC_TYPES` + FE 결재함 딥링크 매핑에 `GENERAL_DRAFT` 추가.

## 7. 회계·타 도메인 영향

**없음(의도)** — 일반 기안은 전표를 만들지 않는다. 금액이 있는 품의는 참고 금액(amount 필드)일 뿐이며,
실집행은 기존 경로(경비청구·PO·전표)로 별도 진행(품의≠집행·이중통제). 문서에 명시해 혼동 방지.

## 8. 테스트 계획

1. build_adhoc 검증(단계 수·중복·기안자 포함 차단·실재 사용자).
2. 생애주기: 기안→상신→2단계 순차 승인→APPROVED·공람 알림·불변 원장 BUILD/APPROVE 기록 확인.
3. 반려·회수(미결재 시만)·재기안 신규 문서번호.
4. 행 스코프: 무관자 조회 403·공람자 read 기록.
5. 결재함(pending_for) 노출·지정 결재자 외 act 거부.
6. 문서번호 연도별 순번·동시성(유니크 충돌 재시도).

## 9. 오픈 결정사항 (구현 전 확인)

- **A. 경계**: §0 — ERP 일반 기안 진행 vs HIS W.Sign 전체 위임.
- **B. 결재선 방식**: v1=자유 지정(ad-hoc)만인지, category별 기본 결재선 룰(기존 ApprovalRule
  doc_type=GENERAL_DRAFT)과 병행할지. ⚠적대검토: **룰 병행(ROLE/DEPT_HEAD 혼합) 채택 시 SELF 행
  스코프가 깨진다**(미래 단계 관여자를 user_id로 SQL 판정 불가·role은 토큰에만) → v1은 **ad-hoc USER
  단계만**으로 확정 권고(스코프 조인 성립). 룰 병행은 v2.
- **C. 공람 시점**: 결재 완료 후만(v1 제안) vs 상신 즉시.
- **D. 수정 범위**: PENDING 중 기안자 수정 금지(v1 제안 — 결재 무결성) 확인.

---

## 10. 적대검토 반영 (2026-07-20 · general-purpose 감사)

기획을 코드와 대조한 적대검토에서 HIGH 3·MED 5·LOW 4 발견. 착수 전 필수 반영분:

### HIGH (착수 전 기획 수정 필수)
- **H-1 STAFF 결재 불가 — 핵심 전제 붕괴**: `common.approvals` read/write에 **STAFF 미등재**
  (`policy.py`) → STAFF를 결재자로 지정해도 worklist·approve EP가 403. ⚠`common.approvals write`를
  STAFF에 개방하면 **룰 없는 경비 단일승인(SoD만)**까지 열려 위험.
  → **수정**: 신규 `common.drafts` `approve` 액션 게이트 + EP 내부는 엔진 `_eligible`(USER 매칭)로만
  결재자 판정. 결재자 지정 후보는 "활성 사용자"이되 결재 게이트는 drafts 전용(경비 게이트와 분리).
- **H-2 첨부 CHECK 제약 — v1.175 경비 첨부가 이미 prod에서 깨져 있음**: ✅ **본 검토 중 즉시
  핫픽스**(마이그 0211·`ck_attachment_entity`에 'expense' 추가). 0211에 `'draft'`도 함께 추가한다.
- **H-3 회수(cancel) 시 결재 인스턴스 처분 미정의**: `pending_for`는 문서상태 무관 인스턴스 PENDING만
  봄 → cancel이 인스턴스 미종결 시 좀비 워크리스트·취소 문서에 APPROVE가 불변원장에 기록. 또 인스턴스
  status CHECK가 `PENDING|APPROVED|REJECTED`뿐(CANCELED 불가).
  → **수정**: 마이그 0212에서 `ck_appinst_status`에 CANCELED 추가. cancel = 인스턴스 **행잠금
  (with_for_update)** → 전 단계 미결재 확인 → 인스턴스 CANCELED + 원장 `CANCEL` 기록. `act()` 진입부에
  문서 상태(도메인) 재검증.

### MED (v1 반영)
- **M-1 원장이 문서 내용 미봉인**: 원장 MAC payload는 메타(doc_type|doc_id|action|actor|acted_at|note)
  뿐·본문/첨부 해시 없음 → 승인 후 본문 변조 무탐지. **수정**: 상신 시 BUILD 원장 note에
  `sha256(title|body|첨부 sha256 목록)` 봉인(§2의 "불변원장이 내용 증거" 주장 성립시킴).
- **M-2 승인 후 첨부 삭제·교체 무방비**: delete guard 미등록 시 기안자가 APPROVED 후 증빙 삭제 가능
  (ITAM은 `register_delete_guard`로 차단). **수정**: DRAFT 외 상태 삭제·업로드 차단 delete guard +
  existence check 등록.
- **M-3 DRAFT 수정·상신 원자성**: PUT(DRAFT)·submit 병행 시 stale-read로 결재자가 안 본 본문 승인 레이스.
  **수정**: PUT·submit 모두 draft 행 `with_for_update`+상태 재검증. build_adhoc에 기존 PENDING 멱등 체크.
- **M-4 결재자 퇴사/비활성 시 문서 표류**: 재지정 수단 없음(유일 탈출=DIRECTOR override). **수정**: 진행
  단계 재지정 EP(ADMIN·원장 감사기록) 추가 또는 운영절차 — §9-E로 승격.
- **M-5 상신·단계진행 알림 부재**: 엔진은 Notification 미생성(pull 전용). H-1과 결합 시 조용히 정체.
  **수정**: 상신 시 1단계, 승인 시 다음 단계 결재자에게 `href` 딥링크 알림(`notifications.href` 존재).

### LOW (반영/기록)
- **L-1** `/approvals/{doc_type}/{id}` 상태 EP 행 스코프 부재(MANAGER ALL이 임의 결재선 조회) — 기안 민감도와
  불일치. 문서 메타 한정이라 후속.
- **L-2** §7 amount 서술 vs §3 모델에 amount 컬럼 없음 — `build_adhoc(amount)`는 인스턴스에만. **수정**:
  `general_drafts.ref_amount`(참고 금액·선택) 추가하거나 amount 서술 삭제. → ref_amount 추가로 확정.
- **L-3** FE 결재함 딥링크 맵에 **EXPENSE도 이미 누락**(경비 건 raw 코드 표시) — GENERAL_DRAFT 추가 시
  EXPENSE도 함께 수리.
- **L-4** DIRECTOR override가 ad-hoc 지정 결재선 무력화 — 일반기안 문맥에선 의미 상이. §9에 수용 명시.

### §9 추가 결정사항 (적대검토 발)
- **E. 채번 시점**: doc_no를 DRAFT 생성 시 vs 상신 시. 미상신/회수 문서 결번 정책 포함 → **상신 시 채번**
  권고(DRAFT 남발·결번 최소화·회수는 미채번이라 결번 없음).
- **F. 결재자 지정 범위**: H-1 귀결 — 활성 사용자 전체 지정 가능하되 **결재 게이트는 drafts.approve 신설**로
  해결(권한 확대 없이).
- **G. 상신 후 첨부 동결**(M-2)·본문 content hash 봉인(M-1) 채택 여부.

## 11. 수정된 마이그레이션 계획
- **0211**(✅ 완료·핫픽스): 첨부 CHECK 'expense'.
- **0212**(선행 — 결재선 요청·관리): `approval_line_templates`·`approval_line_requests`.
- **0213**(본 기능): `general_drafts`·`draft_viewers` 생성 + `ck_appinst_status`에 **CANCELED** 추가
  + 첨부 CHECK에 **'draft'** 추가 + `general_drafts.ref_amount` 포함.
