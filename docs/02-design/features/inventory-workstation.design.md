# 재고 워크스테이션 설계서 (M1 — 바코드 필드·조회·입고·라벨)

> **범위**: 기획 inventory-workstation.plan.md의 **M1** — ① `Item.barcode` ② 품목 조회(스캔) ③ 입고
> ④ 품목/로트 라벨 출력 + 전용 워크스테이션 화면(조회·입고 모드).
> 기본값(협의 전): 바코드=**자체발번 Code128**(미지정 시 item.code 사용), 입력=**키보드 웨지 스캐너**,
> 출력=**브라우저 인쇄**(클라이언트 jsbarcode 렌더). GS1-128·카메라·ZPL은 후속(M2+).
> **작성일**: 2026-06-16 · **버전**: v0.1 · **연관 Plan**: inventory-workstation.plan.md, scm 서비스 재사용

---

## 1. 데이터 (G-1)
- `scm.items.barcode VARCHAR(40) UNIQUE NULL` 추가(마이그 0067). 미입력 시 식별은 `code`로 폴백.
- 로트 라벨은 별도 컬럼 불요 — 출력 시 `item.code|lot_no|expiry` 또는 GS1(후속)로 인코딩.

## 2. 백엔드 (기존 서비스 재사용)
- `ItemBody`에 `barcode: str | None` 추가, `create_item` 반영. `list_items` 반환에 barcode 포함.
- **품목 조회**: `GET /inventory/items/lookup?q=` — barcode(우선) 또는 code 정확일치로 1품목 해소 →
  `{item(id·code·name·unit·is_narcotic·barcode·safety), stock, lots[lot_no·expiry_on·remaining]}`.
  미발견 404. (current_stock·lots는 기존 쿼리 재사용)
- **바코드 설정**: `PATCH /inventory/items/{id}` — barcode(및 safety_stock) 수정(ADMIN/부서장).
- **입고**: 기존 `POST /inventory/movements/receive`(item_id·lot_no·expiry_on·qty·moved_on) 그대로 사용.
- 권한: 조회=scm.items read, 입고/수정=scm.items write. 마약류 품목은 입고 시 안내(수불부 경로는 M3).

## 3. 화면 — `/inventory/workstation` (G-4)
- 스캐너 친화 단일 화면. 상단 **모드 탭(조회 / 입고)**, 항상 포커스된 **스캔 입력창**(Enter/Tab=확정).
- **조회 모드**: 스캔/입력 → lookup → 품목·현재고·안전재고 미달 경고·로트별 잔량/유효기간(임박 강조).
  품목 라벨 인쇄 버튼.
- **입고 모드**: 스캔 → 품목 표시 → 로트번호·유효기간·수량 입력 → ‘입고’ → 성공 후 **로트 라벨 인쇄** 옵션·
  연속 입고(입력창 재포커스). 발주 기반 검수는 M3.
- **라벨**: 숨김 라벨 영역에 jsbarcode(Code128)로 바코드 + 품명/코드/로트/유효기간 렌더 → `window.print()`.
- UX: 큰 버튼·성공/오류 색·음성 피드백(후속), 최근 작업 로그.

## 4. 메뉴
- 카탈로그 `재고` 그룹에 `("/inventory/workstation", "재고 워크스테이션", _MGR)` 추가.

## 5. 테스트
- lookup(바코드/코드 일치·미발견 404), barcode PATCH(중복 unique), 입고 라운드트립(기존 receive). 웹 build.

## 6. 구현 순서
1. 마이그 0067 + 모델 + ItemBody/lookup/PATCH (백엔드)
2. 워크스테이션 화면(조회·입고) + jsbarcode 라벨 + 메뉴
3. 테스트·배포. 이후 M2(출고·실사·GS1·스캔파서)·M3(마약류·검수).

> 협의값(바코드 체계·라벨규격·스캐너)이 확정되면 기본값을 대체. 하드웨어(스캐너·라벨프린터)는 외부.
