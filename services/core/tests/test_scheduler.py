"""워커 스케줄러 — 잡 레지스트리 스냅샷(P0-2 하트비트·P1-3 레지스트리 회귀)."""
from erp.workers.scheduler import build_scheduler


def test_scheduler_registers_expected_jobs():
    """build_scheduler가 핵심 잡을 등록 — 하트비트(P0-2)·인박스·마감·마트 + 정기업무 메타잡."""
    ids = {j.id for j in build_scheduler().get_jobs()}
    # P0-2 워커 생존 하트비트(딥 헬스가 검사)
    assert "heartbeat" in ids
    # 재무·운영 핵심 잡 회귀 방지(실수 삭제 감지)
    for jid in ("backup", "inbound_consume", "access_log_flush", "appeal_deadline"):
        assert jid in ids, jid
    # 정기업무 메타잡(v1.247.0 컷오버) — enabled AUTO 실행·MANUAL 물질화
    assert "recurring" in ids
    # 레지스트리로 이관된 잡은 APScheduler에서 제거(이중실행 방지·job_recurring이 실행)
    for jid in ("contract_lifecycle", "annual_leave_grant", "patient_accrual", "prereview_sync",
                "balance_checkpoint", "ledger_integrity", "budget_alerts",
                "approval_ledger_verify", "license_expiry", "retention_sweep",
                "itam_cost_review", "refresh_bi_marts"):
        assert jid not in ids, f"{jid} 컷오버됐어야 함(레지스트리 실행)"
