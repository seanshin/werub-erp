"""0226 — 결정 대기함(pending_decisions): 사람 결정 항목의 관리 기능화.

감사/기획 과정에서 "사람 결정"으로 승격된 항목(명세 §9류)이 문서·메모리에 흩어져 유실되던 것을
DB 관리 기능으로 — 항목 등록·선택지/권고 제시·결정 기록(감사추적)·보류/재오픈.
Phase0 불변식 감사 §5의 6건을 시드(ON CONFLICT DO NOTHING·재실행 안전).

Revision ID: 0226
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0226"
down_revision = "0225"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pending_decisions",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("key", sa.String(64), nullable=False),
        sa.Column("category", sa.String(20), nullable=False, server_default="GENERAL"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("options", sa.JSON(), nullable=False),        # [{key,label,description,recommended}]
        sa.Column("context_ref", sa.String(300), nullable=True),  # 근거 문서 경로/링크
        sa.Column("status", sa.String(10), nullable=False, server_default="PENDING", index=True),
        sa.Column("decided_option", sa.String(40), nullable=True),
        sa.Column("decided_note", sa.String(1000), nullable=True),
        sa.Column("decided_by", sa.BigInteger(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.BigInteger(), nullable=True),   # NULL=시스템(감사) 등록
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
                  nullable=False),
        sa.UniqueConstraint("key", name="uq_pending_decision_key"),
        sa.CheckConstraint("status IN ('PENDING','DECIDED','DEFERRED')", name="ck_pdec_status"),
        schema="common",
    )
    conn = op.get_bind()
    seeds = [
        ("phase0-ai-pseudonymization", "PRIVACY", "AI 페이로드 가명처리 적정성",
         "외부 AI(weve) 전송 페이로드는 직접식별자(차트·주민·전화)를 차단하지만, 상병코드 조합+진료일"
         "+약품 성분은 소규모 병원에서 준식별자가 될 수 있습니다. 현행 수준을 비식별로 충분하다고 "
         "판단할지, 진료일 정밀도를 낮출지(월 단위 — 고시 시행여부 필터 정확도와 상충) 결정이 필요합니다.",
         [{"key": "KEEP", "label": "현행 유지", "description": "직접식별자 차단+실데이터 대조로 충분 판단(판단 근거를 결정 메모에 기록)", "recommended": True},
          {"key": "MONTH", "label": "진료일 월 단위 강등", "description": "재식별 위험 추가 축소 — 사전심사 시행여부 필터 정확도 일부 저하"},
          {"key": "OFF", "label": "외부 AI 전송 중단", "description": "로컬 검색만 사용(AI 평가 기능 비활성)"}],
         "docs/03-analysis/architecture-invariants-audit-erp-2026-07-23.analysis.md §5-1"),
        ("phase0-weve-contract", "PRIVACY", "example.org AI 서버의 계약적 지위",
         "AI 서버는 코드상 외부 호스트입니다. 동일 벤더(WeRU.B) 운영이라도 개인정보 처리위탁 계약"
         "·국외이전 여부·보관 위치 확인이 있어야 외부 전송 허용 범위가 확정됩니다.",
         [{"key": "CONTRACT", "label": "위수탁 계약 확인·체결", "description": "계약서/리전 확인 후 현행 유지", "recommended": True},
          {"key": "GATE_OFF", "label": "확인 전까지 AI 게이트 OFF", "description": "weve 키 제거로 전송 중단"},
          {"key": "ACCEPT", "label": "현행 수용", "description": "동일 벤더 내부 처리로 간주(리스크 수용을 메모에 기록)"}],
         "docs/03-analysis/architecture-invariants-audit-erp-2026-07-23.analysis.md §5-2"),
        ("phase0-tax-medical-ep", "ARCH", "연말정산 의료비 EP의 모듈 소속",
         "tax 모듈에서 유일하게 환자 chart_no+실명을 다루는 연말정산 의료비 EP를 patient 모듈로 "
         "이관하면 tax가 location-free가 됩니다(장래 tier 분리 유리). 이관 비용은 소규모입니다.",
         [{"key": "MOVE", "label": "patient로 이관", "description": "tax location-free화 — 소규모 리팩터링", "recommended": True},
          {"key": "KEEP", "label": "현행 유지", "description": "tax=PHI-부분으로 매니페스트 유지"}],
         "docs/05-reference/erp-tier-manifest.md"),
        ("phase0-chartno-leaks", "ARCH", "GL 전표 memo·위탁재고의 chart_no 처리",
         "finance 전표 memo와 scm 위탁재고 컬럼에 환자 식별자가 남아 tier 순도를 해칩니다. "
         "회계/재고 추적성과의 트레이드오프 결정이 필요합니다.",
         [{"key": "WITH_PREF", "label": "patientRef 전환 시 일괄 치환", "description": "HIS 가명 토큰 도입과 함께 이관 배치로 치환", "recommended": True},
          {"key": "NOW", "label": "즉시 제거/치환", "description": "추적성 일부 저하 감수"},
          {"key": "KEEP", "label": "현행 유지", "description": "동거 배포 전제 수용(분리 시 재검토)"}],
         "docs/05-reference/erp-tier-manifest.md 경계 누수"),
        ("phase0-backup-offsite", "INFRA", "오프사이트 백업 목적지의 tier 소속",
         "암호화 백업본이라도 병원 경계 밖 반출로 볼지, 목적지 유형(별도 SSH 호스트/국내 오브젝트 "
         "스토리지)을 무엇으로 할지 — 기존 실배포 백로그(P2-1)와 동일 결정입니다.",
         [{"key": "SSH_LOCAL", "label": "병원 소유 원격지(SSH)", "description": "Data-Local 경계 내 유지 — 두 번째 서버 필요", "recommended": True},
          {"key": "OBJ_KR", "label": "국내 오브젝트 스토리지", "description": "암호문 반출 수용(키 분리 보관)"},
          {"key": "DEFER", "label": "보류(로컬만)", "description": "실배포 시점 일괄 결정(현행)"}],
         "docs/01-plan/production-deploy-backlog.md P2-1"),
        ("phase0-audit-targetid", "PRIVACY", "감사로그 target_id의 환자 식별자 취급",
         "AccessLog/AuditLog의 target_id에 chart_no가 참조로 남을 수 있습니다. 이를 PHI로 취급해 "
         "가명 치환할지(patientRef 도입 시 일괄이 효율적) 결정이 필요합니다.",
         [{"key": "WITH_PREF", "label": "patientRef 도입 시 일괄", "description": "가명 토큰으로 치환·감사 추적성 유지", "recommended": True},
          {"key": "HASH_NOW", "label": "즉시 해시 치환", "description": "추적 시 역조회 불편 감수"},
          {"key": "KEEP", "label": "현행 유지", "description": "감사로그는 접근통제로 보호(불변 트리거·역할 제한)"}],
         "docs/03-analysis/architecture-invariants-audit-erp-2026-07-23.analysis.md §5-6"),
    ]
    for key, cat, title, question, options, ref in seeds:
        conn.execute(sa.text(
            "INSERT INTO common.pending_decisions (key, category, title, question, options, "
            "context_ref) VALUES (:k, :c, :t, :q, :o, :r) ON CONFLICT (key) DO NOTHING"),
            {"k": key, "c": cat, "t": title, "q": question,
             "o": json.dumps(options, ensure_ascii=False), "r": ref})


def downgrade() -> None:
    op.drop_table("pending_decisions", schema="common")
