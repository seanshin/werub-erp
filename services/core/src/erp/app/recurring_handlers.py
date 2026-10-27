"""정기업무 AUTO 핸들러 — 기존 스케줄러 잡을 레지스트리로 이관(도메인 서비스 재사용·로직 중복 없음).

각 핸들러는 러너가 준 session에서 **스케줄러 job_*와 동일한 도메인 서비스**를 호출한다(app→domain
허용). 서비스가 단일 진실원이라 두 경로가 갈라지지 않는다. import 시 자동 등록되며, core(create_app)와
worker-batch(scheduler)가 모두 로드하므로 어느 프로세스에서 실행하든 has_handler=True.

1차 이관(순수 서비스 호출): contract_lifecycle·annual_leave_grant·patient_accrual·prereview_sync.
2차 이관(알림 동치=_notify_roles): budget_alerts·balance_checkpoint·ledger_integrity·
approval_ledger_verify. 3차 이관(월간·기존 _startup_catchup 대체=러너 catchup): cost_review·
retention_sweep. 4차 이관: license_expiry(이메일=ctx post_commit_emails·러너가 커밋 후 발송)·
bi_marts(일괄 갱신은 marts.refresh_all_marts로 추출·마트별 세이브포인트 격리). 기획서 R9 **완결** —
APScheduler 잔류는 인프라·연동잡(heartbeat·inbound·his_sync·backup 등)만.
**AI_ASSISTED**: `ops.ar_review_weekly`(1호·주간 미수금)·`tax.vat_quarterly`(2호·부가세 초안) —
다른 핸들러와 달리 **부작용이 없다**. 읽어서 초안만 만들고 `prepare_ai_assisted`가 결정 대기함에
큐잉하며, 사람이 승인해야 확정된다(부가세는 집계 초안까지만 — 신고·납부는 세무사 확정 후 사람이).

컷오버 절차: ①핸들러+시드 추가 ②scheduler build_scheduler의 대응 add_job(잡 함수 포함, 테스트
미커버 시) 제거 — seed_migrated_jobs가 신규 task를 enabled=True(모델 기본)로 생성해 즉시 활성.
"""
from erp.common.recurring import register_handler


async def _notify_roles(session, roles, ntype: str, title: str, body: str, href: str,
                        dedup: str | None = None) -> int:
    """대상 역할 사용자에 인앱 알림 — 러너 세션에서 add(잡별 커밋 포함). 반환=발송 수.

    dedup="type": 미확인 동일 type 알림 있으면 skip · dedup="body": 동일 type+body면 skip
    (일일 배치 반복 소음 방지 — 원 잡의 디둡 동치).
    """
    from sqlalchemy import select

    from erp.common.models import Notification, User
    ids = (await session.execute(select(User.id).where(User.role.in_(roles)))).scalars().all()
    added = 0
    for uid in ids:
        if dedup:
            q = (select(Notification.id).where(
                Notification.user_id == uid, Notification.type == ntype,
                Notification.read_at.is_(None)))
            if dedup == "body":
                q = q.where(Notification.body == body)
            if await session.scalar(q.limit(1)):
                continue
        session.add(Notification(user_id=uid, type=ntype, title=title, body=body, href=href))
        added += 1
    return added


async def _notify_admins(session, ntype: str, title: str, body: str, href: str) -> int:
    """ADMIN 전원 알림(무결성/검증 경보)."""
    return await _notify_roles(session, ["ADMIN"], ntype, title, body, href)


@register_handler("ops.consistency_audit")
async def consistency_audit(session, period_key, ctx):
    """운영 정합성 점검 자동 실행 — 이상이 있으면 관리자에게 알린다.

    `/admin/consistency`는 **사람이 열어봐야** 보인다. 이번 세션의 정합 결함들이 전부 우연히
    드러난 것이었으므로, 열어보지 않아도 이상이 사람에게 닿게 한다.

    각 검사는 정상 상태에서 반드시 OK가 되는 불변식이라 **정상이면 알림이 없다**(영구 발화 금지).
    dedup='body'로 같은 내용이 매 주기 반복 발송되는 소음도 막는다 — 상태가 바뀌면 body가 바뀌어
    다시 알린다.
    """
    from erp.app.consistency_service import run_all
    out = await run_all(session)
    bad = [i for i in out["items"] if i["status"] in ("FAIL", "WARN")]
    if not bad:
        return {"overall": "OK", "checked": len(out["items"]), "notified": 0}
    lines = [f"· [{i['status']}] {i['title']} — {i['summary']}" for i in bad]
    n = await _notify_roles(
        session, ["ADMIN"], "CONSISTENCY",
        f"운영 정합성 이상 {len(bad)}건",
        "\n".join(lines)[:900],
        "/admin/consistency", dedup="body")
    return {"overall": out["overall"], "checked": len(out["items"]),
            "failed": [i["key"] for i in bad], "notified": n}


@register_handler("ops.ar_review_weekly")
async def ar_review_weekly(session, period_key, ctx):
    """주간 미수금 점검 **초안** — AI_ASSISTED 1호(결정 대기함 승인 후 확정).

    부작용이 없다: 읽기만 하고 독촉·상각·전표를 만들지 않는다. 사람이 `/admin/decisions`에서
    승인해야 `sync_ai_decisions`가 run을 DONE으로 확정한다. AI 보조를 이 업무부터 시작한 근거는
    ① 데이터가 전부 ERP 안에 있고 ② **법정 신고가 아니라 틀려도 확정 전에 잡히며**
    ③ 주간이라 피드백 루프가 빨라서다(HR 법정신고 3종은 의도적으로 제외 — 금액은 이미
    결정론적 엔진이 산출하고 AI가 끼면 책임 소재만 흐려진다).

    초안 내용: 연령 버킷 · 지난주 대비 증감 · 우선 대응 후보(고액 장기연체 상위).
    """
    from datetime import timedelta

    from sqlalchemy import select

    from erp.common.kst import kst_today
    from erp.finance.models import Receivable
    from erp.finance.settlement_service import SettlementService

    today = kst_today()
    svc = SettlementService()
    now = await svc.aging(session, as_of=today, kind="AR")
    prev = await svc.aging(session, as_of=today - timedelta(days=7), kind="AR",
                           reconstruct=True)          # 지난주 말 기준 재구성(상태 소급 오염 제거)

    b, pb = now["buckets"], prev["buckets"]
    overdue = sum(v for k, v in b.items() if k != "current")
    prev_overdue = sum(v for k, v in pb.items() if k != "current")

    rows = (await session.scalars(select(Receivable).where(
        Receivable.status.notin_(("COLLECTED", "WRITTEN_OFF"))))).all()
    # 우선 대응 후보 — 연체일 × 잔액이 큰 순(금액만·기간만 보는 것보다 실제 회수 우선순위에 가깝다)
    cand = []
    for r in rows:
        out = float(r.amount) - float(r.collected_amount or 0)
        days = (today - r.due_on).days if r.due_on else 0
        if out > 0 and days > 0:
            cand.append({"party": r.party, "outstanding": round(out), "overdue_days": days,
                         "source": r.source_module})
    cand.sort(key=lambda x: x["outstanding"] * x["overdue_days"], reverse=True)
    top = cand[:10]

    delta = round(overdue - prev_overdue)
    trend = "증가" if delta > 0 else "감소" if delta < 0 else "보합"
    summary = (f"연체 잔액 {round(overdue):,}원({trend} {abs(delta):,}) · "
               f"90일 초과 {round(b['90+']):,}원 · 대응 후보 {len(cand)}건")
    return {
        "summary": summary,
        "counts": {"overdue_total": round(overdue), "over_90d": round(b["90+"]),
                   "delta_vs_last_week": delta, "candidates": len(cand)},
        "question": (
            f"[{period_key}] 주간 미수금 점검 초안입니다.\n\n"
            f"· 연체 잔액 {round(overdue):,}원 (지난주 대비 {trend} {abs(delta):,}원)\n"
            f"· 버킷: 1-30일 {round(b['1-30']):,} / 31-60일 {round(b['31-60']):,} / "
            f"61-90일 {round(b['61-90']):,} / 90일+ {round(b['90+']):,}\n"
            f"· 우선 대응 후보(연체일×잔액 상위 {len(top)}건):\n"
            + "\n".join(f"  - {c['party']} {c['outstanding']:,}원 · {c['overdue_days']}일 경과"
                         for c in top)
            + "\n\n검토 후 승인하면 이번 주 점검이 완료 처리됩니다. "
              "독촉·분납·대손은 이 승인으로 실행되지 않으며 /patients/receivables에서 별도 수행합니다."),
        "context_ref": "/patients/receivables",
    }


@register_handler("tax.vat_quarterly")
async def vat_quarterly_draft(session, period_key, ctx):
    """부가가치세 신고 **초안** — AI_ASSISTED 2호(결정 #8 권고 2순위·초안까지만).

    부작용이 없다: TaxService.vat_summary(세금계산서 raw 집계)를 읽어 초안만 만든다.
    ⚠ 승인은 "초안 검토 완료" 처리일 뿐 **신고·납부를 실행하지 않는다** — 공통매입세액 안분·
    공제율·신고서식은 세무사 확정 영역이고, 신고는 홈택스에서 사람이 한다(결정 #8: 법정 신고에
    AI가 끼면 책임 소재만 흐려지므로 금액 확정은 넘기지 않는다).

    period_key는 **완결된 분기**다(앵커=분기말+25일·prepare가 due_period로 선정).
    """
    from datetime import date

    from erp.common.recurring import period_bounds
    from erp.tax.service import TaxService

    year, q = int(period_key[:4]), int(period_key[-1])          # "2026-Q2"
    start, end = period_bounds("QUARTERLY", date(year, (q - 1) * 3 + 1, 1))
    s = await TaxService().vat_summary(session, start=start, end=end)

    sale_tax, sale_ex = s["sales"]["taxable"], s["sales"]["exempt"]
    buy_tax = s["purchases"]["taxable"]
    payable = s["payable_estimate"]
    return {
        "summary": (f"매출세액 {round(s['output_vat']):,}원 · 매입세액 {round(s['input_vat']):,}원 · "
                    f"납부예상(안분 전) {round(payable):,}원"),
        "counts": {"output_vat": round(s["output_vat"]), "input_vat": round(s["input_vat"]),
                   "payable_estimate": round(payable),
                   "invoices": sale_tax["count"] + sale_ex["count"] + buy_tax["count"]},
        "question": (
            f"[{period_key}] 부가가치세 신고 기초자료 초안입니다 (과세기간 {start}~{end}).\n\n"
            f"· 과세 매출: 공급가 {round(sale_tax['supply']):,}원 · "
            f"세액 {round(sale_tax['vat']):,}원 ({sale_tax['count']}건)\n"
            f"· 면세 매출(의료용역 §26①5): {round(sale_ex['supply']):,}원 "
            f"({sale_ex['count']}건 — 신고 대상 아님·참고)\n"
            f"· 매입: 공급가 {round(buy_tax['supply']):,}원 · "
            f"세액 {round(buy_tax['vat']):,}원 ({buy_tax['count']}건)\n"
            f"· 납부예상세액(안분 전 raw): {round(payable):,}원\n\n"
            "⚠ 공통매입세액 과세·면세 안분 전 세금계산서 raw 집계입니다. "
            "승인하면 이번 분기 초안 검토가 완료 처리될 뿐이며, 신고·납부는 실행되지 않습니다 — "
            "공제율·신고서식은 세무사 확정 후 홈택스에서 별도 진행합니다."),
        "context_ref": "/tax",
    }


@register_handler("scm.contract_lifecycle")
async def contract_lifecycle(session, period_key, ctx):
    """위수탁계약 만기 자동 전이 — ACTIVE→EXPIRING/EXPIRED·auto_renew (job_contract_lifecycle)."""
    from erp.scm.contract_service import OutsourcingContractService
    return await OutsourcingContractService().run_lifecycle_transitions(session)


@register_handler("hr.annual_leave_grant")
async def annual_leave_grant(session, period_key, ctx):
    """재직 전원 당해연도 연차 자동부여(멱등·미부여자만) — job_annual_leave_grant."""
    from erp.common.kst import kst_today
    from erp.hr.leave_service import LeaveService
    return await LeaveService().grant_annual_all(session, year=kst_today().year)


@register_handler("patient.accrual")
async def patient_accrual(session, period_key, ctx):
    """환자 본인부담 발생주의 미수금(당월·전월) — 게이트 OFF면 no-op. job_patient_accrual."""
    from datetime import timedelta

    from erp.common.kst import kst_today
    from erp.config import get_settings
    from erp.patient.receivable_service import PatientReceivableService
    if not get_settings().patient_accrual_enabled:
        return {"skipped": "gate_off"}
    today = kst_today()
    prev = today.replace(day=1) - timedelta(days=1)
    svc = PatientReceivableService()
    posted = 0
    warnings: list = []
    for ym in (today.strftime("%Y-%m"), prev.strftime("%Y-%m")):
        out = await svc.accrue_all(session, care_month=ym)
        posted += out.get("posted", 0) or 0
        for w in out.get("warnings") or []:
            if w["key"] not in {x["key"] for x in warnings}:
                warnings.append(w)
    # 발생주의가 도는데 정합 이상이 있으면 관리자에게 올린다 — 화면을 열지 않아도 닿게.
    # dedup='body'라 상태가 그대로면 반복 발송되지 않고, 바뀌면 다시 알린다.
    notified = 0
    if warnings:
        notified = await _notify_roles(
            session, ["ADMIN"], "CONSISTENCY",
            f"발생주의 전기 중 정합 이상 {len(warnings)}건",
            "\n".join(f"· {w['title']} — {w['summary']}" for w in warnings)[:900],
            "/admin/consistency", dedup="body")
    return {"posted": posted, "warnings": warnings, "notified": notified}


@register_handler("finance.balance_checkpoint")
async def balance_checkpoint(session, period_key, ctx):
    """계정 잔액 체크포인트 적재 + 자기검증 — 불일치 시 ADMIN 경보. job_balance_checkpoint."""
    from datetime import date, timedelta

    from erp.common.kst import kst_today
    from erp.finance.statements import StatementService
    ss = StatementService()
    today = kst_today()
    prev_end = date(today.year, today.month, 1) - timedelta(days=1)   # 전월 말일(안정 경계)
    await ss.refresh_balance_checkpoint(session, through=prev_end)
    chk = await ss.verify_balance_checkpoint(session, as_of=today)
    healed = False
    if not chk["ok"]:
        # 자기치유 (2026-10-09 전체 흐름 검토 D·HIGH-2) — 종전엔 경보만 보내고 틀린 체크포인트를
        # 그대로 둬, 다음 증분이 그 위에 쌓이며 재무상태표가 영구히 틀렸다("재계산 필요"라고 적었지만
        # 재계산 수단이 없었다). 불일치면 전 경계를 재적재하고 다시 검증 — 그래도 틀릴 때만 경보.
        first_mismatch = chk.get("mismatched")
        await ss.rebuild_balance_checkpoints(session)
        chk = await ss.verify_balance_checkpoint(session, as_of=today)
        healed = chk["ok"]
        if healed:
            return {"ok": True, "healed": True, "mismatched_before": first_mismatch}
    if not chk["ok"]:
        await _notify_admins(session, "BALANCE_CHECKPOINT_MISMATCH",
                             "잔액 체크포인트 불일치 — 확인 필요",
                             f"체크포인트 재적재 후에도 체크포인트+델타≠전구간 "
                             f"(상이 계정 {chk.get('mismatched')}) — 집계 로직 점검 필요",
                             "/finance-statements")
    return {"ok": chk["ok"], "healed": healed, "mismatched": chk.get("mismatched")}


@register_handler("finance.budget_alerts")
async def budget_alerts(session, period_key, ctx):
    """예산 초과 능동 경보 — OVER 존재 시 재무역할에 알림(미확인 디둡). job_budget_alerts."""
    from erp.common.kst import kst_today
    from erp.finance.report_service import FinanceReportService
    year = kst_today().year
    rows = await FinanceReportService().budget_execution(session, fiscal_year=year)
    over = [r for r in rows if r["status"] == "OVER"]
    warning = [r for r in rows if r["status"] == "WARNING"]
    if not over:
        return {"over": 0}
    top = sorted(over, key=lambda r: r["available"])[:5]
    detail = " · ".join(f"부서{r['department_id']} {r['account_code']} 초과 {-r['available']:,.0f}원"
                        for r in top)
    body = (f"{year}년 예산 초과 {len(over)}건(경고 {len(warning)}건) — {detail}"
            + (" 외" if len(over) > 5 else ""))
    added = await _notify_roles(session, ["ADMIN", "DIRECTOR", "FINANCE_MANAGER"], "BUDGET_OVER",
                                f"예산 초과 경보 — {len(over)}건", body, "/finance", dedup="type")
    return {"over": len(over), "notified": added}


async def _sign_seal_anchor(session, anchor_out: dict) -> dict | None:
    """자금결재 원장 최신 앵커를 sign 블록체인에 시점봉인(B안) — 외부 독립 증명(TSA).

    앱 키 보유자가 ERP 체인+앵커를 통째로 재작성하는 잔여 위협을 외부 시점봉인으로 봉쇄.
    해시·시퀀스만 제출(PHI·금액 없음 — sign 계약 준수). HTTP는 15s 타임아웃 바운드.

    **이번 실행에 앵커가 생겼는지가 아니라, sign에 봉인된 마지막 seq가 ERP 최신 앵커보다 뒤처졌는지**로
    제출을 정한다. 종전엔 `created`일 때만 제출해, 제출이 한 번 실패하면 그 앵커는 다시는
    제출되지 않았다. sign 조회가 안 되면 이번에 만든 앵커만 제출한다(중복 봉인보다 누락이 나쁘다).
    """
    from erp.config import get_settings
    if not get_settings().sign_api_key:
        return {"skipped": "SIGN_API_KEY 미설정"} if anchor_out.get("created") else None
    from sqlalchemy import select as _s

    from erp.common.models import ApprovalLedgerAnchor
    from erp.integrations import sign_client
    latest = await session.scalar(
        _s(ApprovalLedgerAnchor).order_by(ApprovalLedgerAnchor.seq_no.desc()).limit(1))
    if latest is None:
        return None
    try:
        sealed = await sign_client.latest_audit_event(
            stream=sign_client.FUND_APPROVAL_STREAM, event_type="LEDGER_ANCHOR")
        sealed_seq = ((sealed or {}).get("data") or {}).get("seq_no")
    except Exception:  # noqa: BLE001 — 조회 불가면 이번 앵커만 제출
        if not anchor_out.get("created"):
            return None
        sealed_seq = None
    if isinstance(sealed_seq, int) and sealed_seq >= latest.seq_no:
        return None                                   # 이미 봉인됨(중복 제출 방지)
    try:
        rc = await sign_client.record_audit_event(
            stream=sign_client.FUND_APPROVAL_STREAM, action="LEDGER_ANCHOR",
            target_type="approval_ledger_anchor", target_ref=f"seq:{latest.seq_no}",
            data={"seq_no": latest.seq_no, "entry_count": latest.entry_count,
                  "head_hash": latest.head_hash}, anchor=True)
        out = {"entry_hash": rc.get("entryHash"), "anchored": bool(rc.get("anchored")),
               "ts_gen_time": (rc.get("anchor") or {}).get("tsGenTime")}
        if not anchor_out.get("created"):
            out["catch_up_seq"] = latest.seq_no       # 지난 실패분 재제출
        return out
    except Exception as exc:  # noqa: BLE001 — sign 불가는 비차단(원장 검증 자체는 유효)
        return {"error": str(exc)[:120]}


SEAL_STALE_HOURS = 48       # 마지막 외부 봉인 이후 허용 지연(일 1회 봉인 → 2회 연속 실패면 경보)


async def _sign_seal_audit(session, svc) -> dict:
    """**봉인값 ↔ ERP 원장 대조** + 미봉인 감시 — B안이 실제로 위협을 봉쇄하게 만드는 부분.

    v1.259.0은 앵커 head를 sign에 제출하기만 했다. 제출된 값을 원장과 **대조하는 주체가 없으면**
    내부 키 보유자가 전 구간을 재작성해도 아무 일도 일어나지 않는다 — 증거만 남고 탐지는 0
    (적대검토 C-H1). 또 봉인 판정 상태가 ERP DB에 있어, 공격자가 스스로 앵커를 INSERT하거나
    SIGN_API_KEY를 지우면 **봉인이 영구히 침묵**하는데 그것을 알리는 신호도 없었다(C-H2).

    여기서 매일:
      ① sign에서 최신 LEDGER_ANCHOR를 읽고 그 seq 지점의 ERP 해시를 재계산해 비교 → 불일치=변조
      ② ERP에 봉인되지 않은 신규 엔트리가 있는데 마지막 봉인이 SEAL_STALE_HOURS를 넘겼으면 지연 경보
    둘 다 ADMIN/DIRECTOR 알림. 조회 실패는 비차단(감사 자체를 막지 않음).
    """
    from erp.config import get_settings
    if not get_settings().sign_api_key:
        return {"skipped": "SIGN_API_KEY 미설정"}
    try:
        from erp.integrations import sign_client
        latest = await sign_client.latest_audit_event(
            stream=sign_client.FUND_APPROVAL_STREAM, event_type="LEDGER_ANCHOR")
    except Exception as exc:  # noqa: BLE001 — sign 조회 불가는 비차단
        return {"error": str(exc)[:120]}
    if latest is None:
        return {"sealed_entries": 0, "note": "외부 봉인 이력 없음"}
    data = latest.get("data") or {}
    seq_no, head = data.get("seq_no"), data.get("head_hash")
    out: dict = {"sealed_seq": seq_no, "occurred_at": latest.get("occurredAt")}
    if isinstance(seq_no, int) and isinstance(head, str):
        actual = await svc.hash_at(session, seq_no)
        out["match"] = actual == head
        if actual != head:
            await _notify_roles(
                session, ["ADMIN", "DIRECTOR"], "APPROVAL_LEDGER_TAMPER",
                "결재 원장 무결성 경보 — 외부 봉인 불일치",
                f"sign에 시점봉인된 seq {seq_no}의 head 해시가 현재 원장과 다릅니다 "
                f"({'해당 지점 부재' if actual is None else '해시 상이'}) — 과거 구간 재작성 의심",
                "/approvals", dedup="body")
            return out
    # 미봉인 지연 — 봉인이 조용히 멈춘 상태를 드러낸다
    from erp.common.kst import as_kst_wallclock, kst_now
    from erp.common.models import ApprovalLedger
    from sqlalchemy import func as _f, select as _s
    last_seq = await session.scalar(_s(_f.max(ApprovalLedger.seq_no)))
    occurred = latest.get("occurredAt")
    stale_h = None
    if isinstance(occurred, str):
        try:
            from datetime import datetime as _dt
            at = as_kst_wallclock(_dt.fromisoformat(occurred.replace("Z", "+00:00")))
            stale_h = (as_kst_wallclock(kst_now()) - at).total_seconds() / 3600
        except ValueError:
            stale_h = None
    out["unsealed_entries"] = max(0, (last_seq or 0) - (seq_no or 0))
    if out["unsealed_entries"] > 0 and stale_h is not None and stale_h > SEAL_STALE_HOURS:
        out["seal_stale_hours"] = round(stale_h, 1)
        await _notify_roles(
            session, ["ADMIN", "DIRECTOR"], "APPROVAL_LEDGER_SEAL_STALE",
            "자금결재 원장 외부 봉인 지연",
            f"미봉인 엔트리 {out['unsealed_entries']}건 · 마지막 봉인 {round(stale_h)}시간 전 — "
            "sign 연동·키 설정을 확인하세요", "/approvals", dedup="type")
    return out


@register_handler("common.approval_ledger_verify")
async def approval_ledger_verify(session, period_key, ctx):
    """결재 불변원장 해시체인 검증 — 통과 시 앵커+sign 시점봉인·외부 봉인 대조·파손 시 경보."""
    from erp.common.approval_ledger import ApprovalLedgerService
    svc = ApprovalLedgerService()
    chk = await svc.verify(session)
    if chk["ok"]:
        anc = await svc.checkpoint(session)
        # 봉인 대상은 반드시 **앵커 행의 head_hash** — 바깥 verify 결과를 쓰면 그 사이 append로
        # 앵커와 다른 값이 외부 체인에 영구 각인될 수 있다(C-H3).
        sealed = await _sign_seal_anchor(session, anc)
        audit = await _sign_seal_audit(session, svc)
        return {"ok": True, **({"sign": sealed} if sealed else {}),
                **({"sign_audit": audit} if audit else {})}
    body = f"해시체인 seq {chk.get('first_broken_seq')} 파손({chk.get('reason')}) — 즉시 확인 필요"
    await _notify_roles(session, ["ADMIN", "DIRECTOR"], "APPROVAL_LEDGER_TAMPER",
                        "결재 원장 무결성 경보 — 변조 의심", body, "/approvals", dedup="body")
    return {"ok": False, "first_broken_seq": chk.get("first_broken_seq")}


@register_handler("finance.ledger_integrity")
async def ledger_integrity(session, period_key, ctx):
    """원장 해시체인 증분 체크포인트 + 전체 검증 — 변조 의심 시 ADMIN 경보. job_ledger_integrity."""
    from erp.finance.ledger_integrity_service import LedgerIntegrityService
    svc = LedgerIntegrityService()
    from sqlalchemy.ext.asyncio import AsyncEngine as _AE
    if isinstance(session.bind, _AE) and session.get_bind().dialect.name == "postgresql":
        # 체크포인트는 **별도 짧은 트랜잭션**에서 — SHARE 잠금은 PG에서 세이브포인트가 아니라 트랜잭션 끝에
        # 풀린다. 같은 트랜잭션에서 아래 전체 검증(O(N))까지 하면 그동안 모든 전기가 멈췄다(2차 검토 D).
        # (엔진 바인딩일 때만 — 커넥션 바인딩 테스트 세션은 같은 세션으로)
        from sqlalchemy.ext.asyncio import AsyncSession as _AS
        async with _AS(bind=session.bind, expire_on_commit=False) as s2:
            async with s2.begin():
                cp = await svc.checkpoint(s2, actor=0)
    else:
        cp = await svc.checkpoint(session, actor=0)
    if cp.get("lock_error"):
        # 수평선 잠금 불가(2026-10-09 전체 흐름 검토 D·HIGH-3) — 매 주기 조용히 건너뛰면 체인이
        # 영영 자라지 않으므로 관리자에게 올린다(행위 주체=관리자·조치=DB 권한 점검).
        await _notify_admins(session, "LEDGER_INTEGRITY", "원장 무결성 체크포인트 중단",
                             f"{cp.get('reason')} — 체크포인트가 쌓이지 않습니다", "/finance-periods")
    chk = await svc.verify(session)
    if not chk["ok"]:
        await _notify_admins(session, "LEDGER_INTEGRITY", "원장 무결성 경보 — 변조 의심",
                             f"체크포인트 #{chk.get('broken_checkpoint_id')} 재계산 불일치 — 즉시 확인 필요",
                             "/finance-periods")
    return {"ok": chk["ok"], "checkpoint_skipped": bool(cp.get("skipped"))}


@register_handler("hr.resigned_access_revoke")
async def resigned_access_revoke(session, period_key, ctx):
    """퇴사일이 도래한 직원의 ERP 계정 차단 — 발급 토큰까지 무효(token_version)."""
    from erp.common.kst import kst_today
    from erp.hr.access_revoke import revoke_due
    return await revoke_due(session, today=kst_today())


@register_handler("hr.license_expiry")
async def license_expiry(session, period_key, ctx):
    """면허 만료·보수교육 기한 경보 + 만료자 권한 차단 — job_license_expiry 이관.

    이메일은 ctx["post_commit_emails"]에 스테이징 → 러너가 잡별 커밋 후 발송(롤백 시 미발송).
    주소는 run.result에 저장하지 않는다(건수만).
    """
    from erp.common.kst import kst_today
    from erp.hr.employee_service import LicenseService
    out = await LicenseService().run_expiry_alerts(session, today=kst_today())
    emails = out.pop("emails")
    ctx.setdefault("post_commit_emails", []).extend(emails)
    out["emails_queued"] = len(emails)
    return out


@register_handler("itam.cost_review")
async def itam_cost_review(session, period_key, ctx):
    """월 1회 IT 구독·클라우드 비용 점검 — period_key=점검 대상 월(전월말+3일 앵커). job_cost_review 이관."""
    from erp.itam.cost_service import CostService
    out = await CostService().run_review(session, actor=0, period=period_key)
    alerts = out["flagged"] + out["missing"]
    if alerts > 0:
        await _notify_admins(session, "ITAM_COST_REVIEW",
                             f"IT 비용 점검 {period_key} — 확인 필요 {alerts}건",
                             f"변동 {out['flagged']} · 미입력 {out['missing']} — /assets/cost 확인",
                             "/assets/cost")
    return out


@register_handler("common.retention_sweep")
async def retention_sweep(session, period_key, ctx):
    """월 1회 보존연한 경과 데이터 식별 — 관리자 승인 후 파기(자동삭제 금지). job_retention_sweep 이관.

    dedup은 **기간 스코프**여야 한다: 구 가드는 "당월 알림 존재"였는데 dedup="type"은 기간 개념이
    없는 `read_at IS NULL` 스코프라, 6월 알림이 미확인으로 남아 있으면 7월 알림이 통째로 억제돼
    파기 대상 누적이 무한히 은폐된다(적대검토 A-M1). body에 대상 기간을 넣고 dedup="body"로
    바꿔 월 단위로 구분한다(같은 달 재실행·catchup 중복은 여전히 억제).
    """
    from erp.app.retention_service import RetentionService
    from erp.common.kst import kst_today
    out = await RetentionService().identify_candidates(session, as_of=kst_today())
    total = out["total_candidates"]
    if total > 0:
        await _notify_roles(session, ["ADMIN"], "RETENTION_SWEEP",
                            "보존연한 경과 데이터 파기 대상",
                            f"[{period_key}] 파기 대상 {total}건 식별 — "
                            "/admin/retention/candidates 확인 후 승인·파기",
                            None, dedup="body")
    return {"total_candidates": total}


@register_handler("analytics.bi_marts")
async def bi_marts(session, period_key, ctx):
    """BI reporting 마트 12종 야간 일괄 갱신(당월·전월) — job_refresh_bi_marts 이관.

    마트별 세이브포인트 격리는 refresh_all_marts 내부(실패분만 failed로 반환·성공분 유지) —
    부분 실패는 DONE·result.failed로 가시화(원 잡의 '격리·계속' 동치).

    단 **전량 실패는 예외로 승격**한다(적대검토 A-M4). 예외를 전부 삼키면 마트가 하루 넘게
    stale인데도 run은 DONE이라 health_signal·deep-health·관리화면 어디에도 신호가 잡히지 않고,
    BI 대시보드만 조용히 옛 숫자를 보여준다. 전량 실패는 개별 마트 문제가 아니라 공통 장애
    (DB·세션·권한)이므로 FAILED로 드러내는 편이 옳다.
    """
    from erp.analytics.marts import refresh_all_marts
    from erp.common.kst import kst_today
    out = await refresh_all_marts(session, today=kst_today())
    if out["ok"] == 0 and out["failed"]:
        raise RuntimeError(f"BI 마트 전량 갱신 실패({len(out['failed'])}종)")
    return out


@register_handler("common.weekly_briefing")
async def weekly_briefing(session, period_key, ctx):
    """정기업무 주간 브리핑(월요일 아침) — 최근 7일 요약을 ADMIN·DIRECTOR에 알림(M4 백로그).

    상세는 run.result(전체 브리핑 JSON) — 알림 body는 핵심 수치만. body 디둡(재실행 소음 방지).
    """
    from erp.app.recurring_service import RecurringRunner
    brief = await RecurringRunner().weekly_briefing(session)
    r7, sig = brief["runs_7d"], brief["signal"]
    body = (f"[{period_key}] 최근 7일 완료 {r7['done']}·실패 {r7['failed']} · "
            f"지연 {len(brief['overdue'])}건 · 신호 {'정상' if sig['ok'] else '이상'}")
    if brief["overdue"]:
        body += " — 지연: " + ", ".join(o["code"] for o in brief["overdue"][:5])
    notified = await _notify_roles(session, ["ADMIN", "DIRECTOR"], "RECURRING_WEEKLY_BRIEF",
                                   "정기업무 주간 브리핑", body, "/admin/recurring-tasks",
                                   dedup="body")
    return {**brief, "notified": notified}


@register_handler("claims.prereview_sync")
async def prereview_sync(session, period_key, ctx):
    """사전심사 일일 전체 sync(당월·전월·멱등 안전망) — job_prereview_sync."""
    from erp.common.kst import kst_today
    from erp.claims.worklist_service import WorklistService
    today = kst_today()
    prev = (today.year - (today.month == 1), (today.month - 2) % 12 + 1)
    months = [today.strftime("%Y-%m"), f"{prev[0]:04d}-{prev[1]:02d}"]
    svc = WorklistService()
    lines = 0
    for ym in months:
        out = await svc.sync_from_billing_lines(session, actor=0, care_month=ym)
        lines += out.get("lines", 0) or 0
    return {"lines": lines, "months": months}
