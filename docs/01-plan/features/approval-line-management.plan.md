# 결재선 요청·관리 기획 v0.1 — 템플릿·요청·프로비저닝 (기안문서 선행 기반)

> **위치**: 일반 전자결재(기안문서) 기획 `general-eapproval-drafts.plan.md`의 **선행 기반**. 기안문서가
> 결재선을 "쓰려면", 그 결재선을 **요청·생성·관리**하는 기능이 먼저 있어야 한다. 본 기획이 그 토대.

## 0. 문제 (현황 — 코드 근거)

현 결재선은 **관리자 직접 생성만** 가능하고 요청·템플릿·재사용 개념이 없다:

| 요소 | 현황 | 코드 근거 | 갭 |
|---|---|---|---|
| 결재룰 정의 | `ApprovalRule`(doc_type·category·금액대별 steps) | `common/models.py:157` | 금액 트리거 매칭 전용·**명명 템플릿 아님** |
| 룰 생성 | ADMIN이 `POST /approvals/rules` 직접 | `app/approval_router.py:54`·`common.approval_rules write`=ADMIN | **부서/사용자 요청 경로 없음** |
| 결재선 선택 | 도메인이 doc_type으로 자동 매칭(`find_rule`) | `approval_engine.py:31` | 사용자가 **골라 쓸 결재선 목록 없음** |
| 개인 결재선 | 없음 | grep 무결과 | 자주 쓰는 결재자 순서 저장 불가 |
| ad-hoc 지정 | 없음(기안문서 기획서가 build_adhoc 제안) | — | 요청·승인 없이 임의 지정만이면 통제 부재 |

**결과**: 기안문서를 ad-hoc(임의 지정)만으로 열면 통제·표준이 없고, 룰 전용이면 유연성이 없다. **중간 계층
= "요청해서 승인받아 만드는 표준 결재선 + 골라 쓰기"**가 비어 있다.

## 1. 핵심 개념

### (1) 결재선 템플릿 `ApprovalLineTemplate`
명명된 재사용 결재선. 문서 상신 시 **골라 쓰는** 단위.
- `scope`: **ORG**(기관 공용·거버넌스 승인 필요) / **PERSONAL**(개인 전용·자유 생성).
- `steps`: 순서 배열 `[{type: ROLE|DEPT_HEAD|USER, value}]` — 기존 엔진 step 규약 재사용.
- `doc_types`: 이 결재선을 쓸 수 있는 문서유형 집합(GENERAL_DRAFT·EXPENSE·PO… 또는 ANY).
- `owner_user_id`(PERSONAL)·`department_id`(ORG 귀속 부서·선택).

### (2) 결재선 요청 `ApprovalLineRequest` — ★"요청하고 만드는" 본체
부서/사용자가 **표준(ORG) 결재선 신설·변경을 요청** → 거버넌스 검토 → 승인 시 템플릿 프로비저닝.
- 요청자·제안 steps·사유·대상 doc_types·요청유형(NEW|MODIFY|RETIRE).
- 상태: `REQUESTED → APPROVED | REJECTED`(+요청자 `CANCELED`).
- 승인 시: NEW=템플릿 생성, MODIFY=대상 템플릿 steps 교체(이력), RETIRE=비활성.
- **검토자**: 결재선 거버넌스 소유자(ADMIN, 또는 신규 역할 위임 — §5).

### (3) 개인 결재선 (PERSONAL 템플릿)
사용자가 자주 쓰는 결재자 순서를 저장·재사용(요청 불요·본인 전용). 상신 시 개인/공용 결재선 목록에서 선택.

## 2. 흐름 (요청→생성→사용)

```
[요청]  부서 담당자: "지출품의는 팀장→부서장→행정원장 3단계 필요" → ApprovalLineRequest(NEW)
   │
[검토]  결재선 관리자: 제안 확인 → 승인/반려 (불변 원장 기록·감사)
   │ (승인)
[생성]  ORG 템플릿 프로비저닝 → 해당 doc_type 결재선 목록에 노출
   │
[사용]  기안자: 문서 상신 시 [공용 결재선 / 개인 결재선 / (허용 시)ad-hoc] 중 선택
   │        → 엔진이 선택 결재선 steps로 인스턴스 build → 기존 act()/워크리스트/원장 그대로
```

## 3. 요청사항 (Requirements) — ★사용자 확인 대상

기능 요구사항(FR). 각 항목은 착수 전 승인/조정 대상:

- **FR-1 결재선 요청 접수**: 부서/사용자가 표준 결재선의 신설·변경·폐기를 사유와 함께 요청(steps 제안 포함).
- **FR-2 요청 검토·승인**: 결재선 관리자가 요청을 검토해 승인/반려. 승인 시 자동 프로비저닝(수기 재입력 없음).
- **FR-3 결재선 템플릿 목록**: doc_type별 사용 가능한 공용 결재선을 상신 화면에서 조회·선택.
- **FR-4 개인 결재선**: 사용자가 개인 결재선을 생성·수정·삭제(본인 전용).
- **FR-5 결재선 적용**: 선택한 결재선 steps로 결재 인스턴스 생성(엔진 재사용)·상신.
- **FR-6 변경 이력·감사**: 결재선 신설/변경/폐기와 요청 결정이 감사로그+불변 원장에 남음.
- **FR-7 유효성**: steps 검증(1~8단계·유형 유효·ROLE/USER value 필수·DEPT_HEAD는 귀속부서 필요·중복 결재자 차단).
- **FR-8 권한 경계**: 요청=전 직원 / 검토·프로비저닝=결재선 관리자 / 개인 결재선=본인.

**요청사항 결정 항목(사용자)**:
1. **R-1 거버넌스 주체**: 결재선 요청 검토·승인을 **ADMIN**이 겸할지, **신규 "결재선 관리자" 역할**을 둘지.
2. **R-2 ad-hoc 허용 범위**: 기안문서에서 임의 결재선(ad-hoc)을 허용할지, **공용/개인 템플릿 선택만** 강제할지
   (통제 강도 결정). 권고: v1은 템플릿 선택 + 개인 결재선. ad-hoc은 옵션(기관 정책).
3. **R-3 doc_type 범위**: 결재선 관리를 GENERAL_DRAFT에만 적용할지, 기존 유형(PO·경비 등)까지 통합할지
   (통합 시 기존 ApprovalRule과의 관계 정리 필요 — §6).
4. **R-4 요청 자체의 결재**: 결재선 요청을 단순 관리자 승인으로 볼지, N단계 결재로 볼지(순환 의존 주의 — §6).
5. **R-5 기존 ApprovalRule 처리**: 금액대별 자동매칭 룰을 템플릿으로 **흡수/공존/유지** 중 무엇으로 할지.

## 4. 데이터 모델 (마이그 0212 — 기안문서보다 선행)

```
common.approval_line_templates
  id · name(표시명) · scope(ORG|PERSONAL) · owner_user_id(PERSONAL)
  department_id(ORG 귀속·선택) · doc_types(JSON 배열 or 'ANY')
  steps(JSON) · active · source_request_id(프로비저닝 출처·선택)
  created_by · created_at · updated_at

common.approval_line_requests
  id · req_no(요청번호) · request_type(NEW|MODIFY|RETIRE)
  target_template_id(MODIFY/RETIRE 대상·선택) · proposed_name · proposed_steps(JSON)
  doc_types(JSON) · reason · department_id(요청 부서)
  status(REQUESTED|APPROVED|REJECTED|CANCELED)
  requested_by · reviewed_by · reviewed_at · review_note · created_at
```
결재 인스턴스는 기존 `approval_instances/steps` 재사용(신규 없음). 템플릿 steps → build 시 복사(스냅샷 —
템플릿 후속 변경이 진행 중 문서에 소급 안 됨).

## 5. 권한 (RBAC)
- `common.approval_lines` `read`: 전 역할(WORKSTATION 제외·공용 결재선 목록·본인 요청/개인 결재선).
- `common.approval_lines` `write`: 전 역할 SELF(요청 접수·개인 결재선 CRUD).
- `common.approval_lines` `admin`: 결재선 관리자(R-1 — ADMIN 기본·신규 역할은 결정 후) — 요청 승인·ORG
  템플릿 프로비저닝/폐기.

## 6. 기존 자산과의 관계 (설계 주의)
- **ApprovalRule(금액대 자동매칭) vs 템플릿(선택형)**: 둘은 트리거 방식이 다르다(자동 vs 선택). v1 권고 =
  **공존** — PO·경비 등 금액 리스크 문서는 기존 자동매칭 유지, 기안문서 등 선택형 문서는 템플릿. 통합(R-3/R-5)은
  결정 후 별도.
- **순환 의존 회피(R-4)**: 결재선 요청을 "일반 기안문서"로 처리하면 기안문서↔결재선 상호 의존. v1 권고 =
  결재선 요청은 **전용 경량 승인**(관리자 단일 승인·불변 원장 기록)으로 분리.
- **불변 원장**: 요청 승인/거부·템플릿 프로비저닝을 원장 action(`LINE_REQUEST`/`LINE_PROVISION`)으로 기록
  (BUILD/APPROVE와 동형·변조 증거).

## 7. 순서(사용자 지침 반영)
1. **본 기획(결재선 요청·관리) 확정** ← 지금. R-1~R-5 결정.
2. 결재선 요청·관리 구현(마이그 0212·엔진 `build_from_template`).
3. 그 위에 기안문서(general-eapproval-drafts) 구현 — 상신 시 결재선 **선택**(ad-hoc은 R-2 결정에 따름).

## 8. 테스트 계획(개요)
요청 접수→승인→템플릿 프로비저닝 / 개인 결재선 CRUD·본인 경계 / 템플릿 steps→build 스냅샷 / 권한 경계
(요청=전직원·승인=관리자) / 유효성(단계·중복·DEPT_HEAD 부서) / 원장 기록.

## 9. 오픈 결정사항
§3 요청사항 R-1~R-5 + 기안문서 기획서 §9(A~G)와 정합. **R-1(거버넌스 주체)·R-2(ad-hoc 허용)·R-5(기존 룰
처리)** 3건이 착수 선행 필수.
