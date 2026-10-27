"use client";
/** 역할별 대시보드 (FR-19) — 운영 알림(일마감·연동·PG) + 알림·이의신청·삭감·재고 위젯. */
import { Num, scaleWon } from "@/components/kpi";
import { todayLocal } from "@/lib/dates";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { api, auth } from "@/lib/api";
import { Badge, Card, Table } from "@/components/ui";
import type { ClaimStatRow, Item } from "@/types";
import { won } from "@/lib/format";

// 운영 상태 카드 — 건수+임계색상+클릭 딥링크 (지금 조치 필요한 것)
function StatCard({ label, value, sub, tone, href }: {
  label: string; value: string; sub?: string;   // sub = 주 수치를 해석하는 보조값(금액·경고)
  tone: "gray" | "green" | "amber" | "red"; href: string;
}) {
  const t = {
    gray: "border-slate-200 text-slate-700", green: "border-emerald-200 text-emerald-700",
    amber: "border-amber-300 bg-amber-50/50 text-amber-700", red: "border-red-300 bg-red-50/50 text-red-700",
  }[tone];
  return (
    <Link href={href} className={`rounded-xl border bg-white p-3 transition hover:shadow-sm ${t}`}>
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-lg font-bold">{value}</p>
      {sub && <p className="text-[11px] text-slate-500">{sub}</p>}
    </Link>
  );
}

// 위젯 쿼리 실패 시 카드 자리 유지(조용한 소실 방지·조치필요 알림 은폐 차단·P0-3)
function ErrCard({ label }: { label: string }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-slate-50 p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="text-sm font-medium text-slate-400">불러오기 실패</p>
    </div>
  );
}

export default function DashboardPage() {
  const role = auth.claims()?.role ?? "STAFF";
  const isManagerUp = ["DIRECTOR", "ADMIN", "MANAGER"].includes(role);
  const isAdmin = role === "ADMIN";
  const isDirectorUp = ["DIRECTOR", "ADMIN"].includes(role);
  const today = todayLocal();
  const ym = today.slice(0, 7);
  const monthEnd = `${ym}-${String(new Date(Number(ym.slice(0, 4)), Number(ym.slice(5, 7)), 0).getDate()).padStart(2, "0")}`;

  // 운영 알림 — 오늘 일마감(원무), 연동 실패/DLQ·공유 PG 슬롯(관리자)
  const { data: closing, isError: closingErr } = useQuery({
    // 일마감 미리보기=patient.closing read(DIRECTOR/ADMIN) — MANAGER는 403이라 호출·카드 자체를 생략
    queryKey: ["dash-closing", today], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ already_closed: boolean; count: number; total: number; net_cash: number;
      self_closing: boolean }>(`/patients/closings/preview?on=${today}`),
  });
  const { data: inbound, isError: inboundErr } = useQuery({
    queryKey: ["dash-inbound"], enabled: isAdmin, retry: 0,
    queryFn: () => api<{ count: number }>("/integrations/his/inbound?status=FAILED"),
  });
  const { data: pg, isError: pgErr } = useQuery({
    queryKey: ["dash-pg"], enabled: isAdmin, retry: 0,
    queryFn: () => api<{ usage_pct: number | null; warning: boolean; total: number; max_connections: number }>(
      "/admin/diagnostics/db-connections"),
  });
  // P2·P3 — 사전심사 미처리(심사 MGR↑), 검진권 미완료 콜백·세금계산서 미대사(DIR/ADMIN)
  const { data: prereview, isError: prErr } = useQuery({
    queryKey: ["dash-prereview"], enabled: isManagerUp, retry: 0,
    queryFn: () => api<{ by_status: { status: string; count: number }[] }>("/claims/pre-review/stats"),
  });
  const { data: voucher, isError: vErr } = useQuery({
    queryKey: ["dash-voucher", ym], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ settlement_callbacks: Record<string, number> }>(
      `/analytics/voucher?date_from=${ym}-01&date_to=${monthEnd}`),
  });
  const { data: tax, isError: txErr } = useQuery({
    queryKey: ["dash-tax", today.slice(0, 4)], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ unmatched: { count: number } }>(`/finance/tax-invoices/summary?year=${today.slice(0, 4)}`),
  });
  const prOpen = (prereview?.by_status ?? []).find((s) => s.status === "OPEN")?.count ?? 0;
  const cbPending = Object.entries(voucher?.settlement_callbacks ?? {})
    .filter(([s]) => s !== "SENT").reduce((a, [, c]) => a + c, 0);
  const taxUnmatched = tax?.unmatched.count ?? 0;

  const { data: notif, isError: notifError } = useQuery({
    queryKey: ["notifications"],
    queryFn: () => api<{ items: { id: number; type: string; title: string; read_at: string | null;
      href: string | null; created_at: string | null }[] }>("/notifications?size=10"),
  });
  const { data: appeals } = useQuery({
    queryKey: ["appeals-widget"],
    queryFn: () => api<{ items: { id: number; status: string; legal_deadline: string; d_day: number }[] }>(
      "/claims/appeals?size=5"),
    enabled: isManagerUp,
    retry: 0,
  });
  const { data: claimStats } = useQuery({
    queryKey: ["claim-stats-widget"],
    queryFn: () => api<{ items: ClaimStatRow[]; automation: { rate: number } }>(
      "/claims/stats?by=dept"),
    enabled: isManagerUp,
    retry: 0,
  });
  const { data: stockItems } = useQuery({
    queryKey: ["stock-widget"],
    queryFn: () => api<{ items: Item[] }>("/inventory/items?size=200"),
    enabled: isManagerUp,
    retry: 0,
  });
  const lowStock = (stockItems?.items ?? [])
    .filter((it) => it.safety_stock > 0 && it.stock < it.safety_stock);

  // 경영·관리 위젯 — 내 결재 대기, 법정기한 임박, 재무 건전성, IT 구독비용
  const { data: worklist, isError: wlErr } = useQuery({
    queryKey: ["dash-approvals"], enabled: isManagerUp, retry: 0,
    queryFn: () => api<{ items: { instance_id: number; doc_type: string; doc_id: number;
      amount: number | null }[] }>("/approvals/worklist"),
  });
  const { data: deadlines, isError: dlErr } = useQuery({
    // 법정기한 통합 뷰=regulatory.compliance read(DIRECTOR/ADMIN·피폭선량 등 전사 민감정보)
    queryKey: ["dash-deadlines"], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ summary: { overdue: number; d7: number; d30: number } }>(
      "/compliance/deadlines?horizon_days=30"),
  });
  const { data: ratios, isError: ratErr } = useQuery({
    queryKey: ["dash-ratios", today.slice(0, 4)], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ current_ratio: number | null; debt_ratio: number | null;
      operating_margin: number | null; basis: { revenue: number; net_income: number } }>(
      `/analytics/financial-ratios?year=${today.slice(0, 4)}`),
  });
  const { data: itOpex, isError: opexErr } = useQuery({
    queryKey: ["dash-itopex", ym], enabled: isDirectorUp, retry: 0,
    queryFn: () => api<{ total_krw: number; by_type: Record<string, number> }>(
      `/itam/opex?period=${ym}`),
  });
  const { data: nursing, isError: nurErr } = useQuery({
    queryKey: ["dash-nursing"], enabled: isManagerUp, retry: 0,
    queryFn: () => api<{ grade: string | null; ratio: number; hospital_type: string;
      simulation: { grade_if_one_leaves: string | null } }>(
      "/regulatory/nursing-grade/monitor"),
  });
  const apprCount = worklist?.items.length ?? 0;
  // 대기 3건이 30만원인지 3억인지에 따라 처리 순서가 바뀐다(응답 amount를 버리고 있었다)
  const apprAmount = (worklist?.items ?? []).reduce((a, i) => a + (i.amount ?? 0), 0);
  const dlOverdue = deadlines?.summary.overdue ?? 0;
  const dlD7 = deadlines?.summary.d7 ?? 0;
  const pctParts = (r: number | null | undefined) =>
    r == null ? { value: "—", unit: "" } : { value: (r * 100).toFixed(1), unit: "%" };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">대시보드</h1>
      {notifError && <p className="text-sm text-red-600">알림을 불러오지 못했습니다</p>}

      {(isManagerUp || isAdmin) && (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {isDirectorUp && (closingErr ? <ErrCard label="오늘 일마감" /> : closing && (
            // "미마감 12건"보다 미마감 금액이 처리 우선순위를 정한다. 본인 단독 입력이면
            // 마감 화면에서 강제 확인이 필요하므로 카드에서 미리 경고한다.
            <StatCard label={`오늘 일마감 (${today.slice(5)})`}
              value={closing.already_closed ? "마감 완료 ✓"
                : `미마감 ${won(closing.total)} (${closing.count}건)`}
              sub={closing.already_closed ? undefined
                : `현금 시재 ${won(closing.net_cash)}${closing.self_closing ? " · 본인 단독 입력" : ""}`}
              tone={closing.already_closed ? "green"
                : closing.self_closing ? "red" : closing.count > 0 ? "amber" : "gray"}
              href="/patients/closing" />
          ))}
          {isAdmin && (inboundErr ? <ErrCard label="연동 실패/DLQ" /> : inbound && (
            <StatCard label="연동 실패/DLQ"
              value={`${inbound.count}건`} tone={inbound.count > 0 ? "red" : "green"}
              href="/admin/integrations" />
          ))}
          {isAdmin && (pgErr ? <ErrCard label="공유 PG 슬롯" /> : pg && (
            <StatCard label="공유 PG 슬롯"
              value={pg.usage_pct != null ? `${pg.usage_pct}% (${pg.total}/${pg.max_connections})` : "—"}
              tone={pg.warning ? "red" : (pg.usage_pct ?? 0) >= 60 ? "amber" : "green"}
              href="/admin/integrations" />
          ))}
          {isManagerUp && (prErr ? <ErrCard label="사전심사 미처리" /> : prereview && (
            <StatCard label="사전심사 미처리"
              value={`${prOpen}건`} tone={prOpen > 0 ? "amber" : "green"}
              href="/claims/pre-review" />
          ))}
          {isDirectorUp && (vErr ? <ErrCard label="검진권 미완료 콜백" /> : voucher && (
            <StatCard label="검진권 미완료 콜백"
              value={`${cbPending}건`} tone={cbPending > 0 ? "amber" : "green"}
              href="/finance-voucher-status" />
          ))}
          {isDirectorUp && (txErr ? <ErrCard label="세금계산서 미대사" /> : tax && (
            <StatCard label="세금계산서 미대사"
              value={`${taxUnmatched}건`} tone={taxUnmatched > 0 ? "amber" : "green"}
              href="/finance-tax-invoices" />
          ))}
          {isManagerUp && (wlErr ? <ErrCard label="내 결재 대기" /> : worklist && (
            <StatCard label="내 결재 대기"
              value={`${apprCount}건`}
              sub={apprAmount > 0 ? won(apprAmount) : undefined}
              tone={apprCount > 0 ? "amber" : "green"}
              href="/approvals" />
          ))}
          {isDirectorUp && (dlErr ? <ErrCard label="법정기한 임박 (30일)" /> : deadlines && (
            <StatCard label="법정기한 임박 (30일)"
              value={dlOverdue > 0 ? `경과 ${dlOverdue}·D7 ${dlD7}` : `D-7 ${dlD7}건`}
              tone={dlOverdue > 0 ? "red" : dlD7 > 0 ? "amber" : "green"}
              href="/compliance" />
          ))}
          {isManagerUp && (nurErr ? <ErrCard label="간호등급 (입원료 가산)" /> : nursing && (
            <StatCard label="간호등급 (입원료 가산)"
              value={nursing.grade ? `${nursing.grade}등급 (비율 ${nursing.ratio})` : "—"}
              tone={nursing.simulation.grade_if_one_leaves &&
                nursing.simulation.grade_if_one_leaves !== nursing.grade ? "amber" : "green"}
              href="/regulatory/nursing-grade" />
          ))}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <Card title="최근 알림">
          <ul className="space-y-2">
            {(notif?.items ?? []).slice(0, 5).map((n) => (
              <li key={n.id} className="flex min-w-0 items-center gap-2 text-sm">
                {/* 긴 유형 코드(RECURRING_TASK_DUE 등)가 줄바꿈 없이 휴대폰 폭을 넘던 것 — 유형은 줄이고 제목은 말줄임 */}
                <span className="max-w-[40%] shrink-0 truncate"><Badge tone={n.read_at ? "gray" : "blue"}>{n.type}</Badge></span>
                {/* href가 응답에 있는데 링크를 안 걸어 알림이 조치로 이어지지 않았다 */}
                {n.href
                  ? <Link href={n.href} className={`min-w-0 truncate hover:underline ${n.read_at ? "text-slate-400" : "text-blue-700"}`}>
                      {n.title}</Link>
                  : <span className={`min-w-0 truncate ${n.read_at ? "text-slate-400" : ""}`}>{n.title}</span>}
                {n.created_at && <span className="ml-auto shrink-0 text-xs text-slate-400">
                  {n.created_at.slice(5, 16).replace("T", " ")}</span>}
              </li>
            ))}
            {!notif?.items.length && <li className="text-sm text-slate-400">알림 없음</li>}
          </ul>
        </Card>
        {isManagerUp && (
          <Card title="이의신청 기한 임박 (FR-40)">
            <Table headers={["#", "상태", "기한", "D-day"]}
              rows={(appeals?.items ?? []).map((a) => [
                a.id, <Badge key="s" tone={a.status === "RECEIVED" ? "blue" : "amber"}>{a.status}</Badge>,
                a.legal_deadline,
                <Badge key="d" tone={a.d_day <= 7 ? "red" : "gray"}>D-{a.d_day}</Badge>,
              ])} empty="진행 중인 이의신청 없음" />
          </Card>
        )}
        {isManagerUp && (
          <Card title="삭감 통계 (진료과별)">
            <Table headers={["진료과", "건수", "삭감액"]}
              rows={(claimStats?.items ?? []).slice(0, 5).map((r) => [
                r.dim, r.count, won(r.amount)])}
              empty="삭감 데이터 없음" />
            <p className="mt-2 text-xs text-slate-500">
              자동 매핑율 <Badge tone="blue">{claimStats?.automation.rate ?? 0}%</Badge> (목표 90%)
            </p>
          </Card>
        )}
        {isManagerUp && (
          <Card title="안전재고 경고 (FR-13)">
            <Table headers={["코드", "품명", "재고", "안전재고"]}
              rows={lowStock.slice(0, 5).map((it) => [
                it.code, it.name,
                <Badge key="s" tone="red">{it.stock}</Badge>, it.safety_stock,
              ])} empty="미달 품목 없음 ✓" />
          </Card>
        )}
        {isDirectorUp && !ratErr && ratios && (
          <Card title={`재무 건전성 (${today.slice(0, 4)})`}>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-sm">
              <div><dt className="text-[11px] text-slate-400">유동비율</dt>
                <dd className="font-semibold"><Num {...pctParts(ratios.current_ratio)} /></dd></div>
              <div><dt className="text-[11px] text-slate-400">부채비율</dt>
                <dd className="font-semibold"><Num {...pctParts(ratios.debt_ratio)} /></dd></div>
              <div><dt className="text-[11px] text-slate-400">의료이익률</dt>
                <dd className="font-semibold"><Num {...pctParts(ratios.operating_margin)} /></dd></div>
              <div><dt className="text-[11px] text-slate-400">당기순이익</dt>
                <dd className="font-semibold"><Num {...scaleWon(ratios.basis.net_income)} /></dd></div>
            </dl>
            <Link href="/finance-statements" className="mt-2 inline-block text-xs text-blue-600">
              재무제표 →</Link>
          </Card>
        )}
        {isDirectorUp && !opexErr && itOpex && (
          <Card title={`IT 구독·서비스 비용 (${ym})`}>
            <p className="text-lg font-bold tabular-nums"><Num {...scaleWon(itOpex.total_krw)} /></p>
            <ul className="mt-1 space-y-1 text-sm">
              {Object.entries(itOpex.by_type)
                .sort((a, b) => b[1] - a[1]).slice(0, 3).map(([t, v]) => (
                  <li key={t} className="flex justify-between text-slate-600">
                    <span>{t}</span><Num {...scaleWon(v)} /></li>
                ))}
              {!Object.keys(itOpex.by_type).length && (
                <li className="text-slate-400">당월 비용 없음</li>)}
            </ul>
            <Link href="/assets/cost" className="mt-2 inline-block text-xs text-blue-600">
              비용 원장 →</Link>
          </Card>
        )}
      </div>
    </div>
  );
}
