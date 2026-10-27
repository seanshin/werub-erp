"""외부 수신 어댑터 인터페이스 (ACL) — design §5.1.

구현체는 Phase 0 확인(M0a: HIS 방식, M0b: 화이트리스트) 후 교체 가능하게 후행.
모든 수신은 integration.* 스테이징 경유 — 코어 직접 쓰기 금지 (design §5.2).
"""
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol


# ── DTO (raw — ACL 변환 전) ─────────────────────────────────
# 환자·수납 raw DTO(RawPatientDTO/RawPaymentDTO) + HISPatientSource 능동수집 Protocol은 제거됨
# (2026-06-30): HIS가 ERP-sync 폴링 EP(`/api/v1/erp/patients|payments`)를 제공하지 않음으로 확정
# (회신서 §B — 수납=`/integration/billing/lines` 재사용, 환자 디렉터리 실수요 없음). live 환자/수납
# 적재는 `his_billing.backfill_lines`가 담당. 스테이징(HISPatientStaging/HISPaymentStaging)→코어
# 소비(patient.ingest.consume_*)는 보존(job_his_sync 드레인·향후 HIS push 재배선 타깃).
@dataclass
class RawStaffDTO:
    external_user_id: str      # Clinic 직원 id — 동기화 멱등키·근태 매핑
    name: str
    email: str | None
    his_role: str | None       # Admin | Doctor | Nurse | Pharmacist | LabTech ...
    department: str | None
    license_no: str | None
    joined_on: date | None


@dataclass
class RawAttendanceDTO:
    employee_no: str
    clock_in: datetime | None
    clock_out: datetime | None
    work_on: date
    # 원천 추적용(스테이징 raw 보존) — 코어 변환 전 메타. 선택 필드(하위호환).
    external_user_id: str | None = None
    name: str | None = None
    department: str | None = None


@dataclass
class RawNoticeFile:
    original_name: str
    content: bytes
    collected_from: str       # 폴더 경로·포털 등 출처


@dataclass
class RawFeeDTO:
    code: str
    code_type: str            # TARIFF | DRUG | MATERIAL
    name: str
    unit_price: float
    price_cap: float | None
    valid_from: date


# ── Protocol (design §5.1 — 구현 후보는 각 docstring) ───────
class GroupwareAttendanceSource(Protocol):
    """그룹웨어 → 출퇴근 기록 (FR-28). 방식: M0a와 함께 확인 (API/파일)."""

    def fetch_attendance(self, since: datetime) -> Iterator[RawAttendanceDTO]: ...


class HiraNoticeSource(Protocol):
    """심평원 심사결과통보서 수집 (FR-38). 구현 후보:
    HIS 청구모듈 폴더 감시(유력) | 수동 업로드(구현됨 — NoticeUploadService, 필수 fallback).
    파서는 G1 Gate — 실물 샘플(M0c) 확보 후."""

    def collect_files(self) -> Iterator[RawNoticeFile]: ...


class FeeMasterSyncSource(Protocol):
    """공공 수가 API → 마스터 동기화 (FR-34). 화이트리스트(M0b) 등록 후 활성.
    소비자: FeeSyncService.apply (멱등·close-then-open — 구현됨)."""

    def fetch_updates(self, since: date) -> Iterator[RawFeeDTO]: ...
