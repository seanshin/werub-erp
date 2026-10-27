"use client";
/** 재무 — 결산·재무제표 (FR-08): 손익계산서·재무상태표·시산표. */
import { todayLocal, localISO } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, apiDownload } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";

interface Line { account_code: string; name: string | null; amount: number }
interface Income { revenues: Line[]; expenses: Line[];
  total_revenue: number; total_expense: number; net_income: number;
  by_function?: { cogs: number; sga: number; other: number; gross_profit: number; operating_income: number } }
interface Operating {
  medical_revenue: number; medical_expense: number; medical_income: number;
  non_medical_revenue: number; non_medical_expense: number;
  pretax_income: number; income_tax: number; net_income: number;
  items: { medical_revenue: Line[]; medical_expense: Line[];
           non_medical_revenue: Line[]; non_medical_expense: Line[]; tax: Line[] };
  note: string;
}
interface Comprehensive { net_income: number; oci_total: number; total_comprehensive_income: number;
  oci_items: { account_code: string; name: string; amount: number }[] }
interface Agg { revenue: number; expense: number; net: number }
interface BSItem { account_code: string; name: string | null; amount: number }
interface Balance { assets: BSItem[]; liabilities: BSItem[]; total_assets: number;
  total_liabilities: number; equity: number; retained_earnings: number; balanced: boolean;
  // kind 미매핑 계정은 자산·부채 양변에서 함께 탈락해 **거짓 균형**을 만든다 —
  // 서버가 이 경고를 별도로 내보내는데 화면이 버리고 있었다(statements.py 주석이 명시).
  unmapped_accounts: string[] }
interface TB { accounts: { account_code: string; name: string | null; kind: string;
  debit: number; credit: number }[]; total_debit: number; total_credit: number; balanced: boolean }
interface EquityChanges { items: { account_code: string; name: string; opening: number;
  increase: number; decrease: number; closing: number }[];
  total_opening: number; total_closing: number; net_income: number; balanced: boolean }

export default function StatementsPage() {
  const now = new Date();
  const y = now.getFullYear();
  const [start, setStart] = useState(`${y}-01-01`);
  const [end, setEnd] = useState(localISO(now));

  const income = useQuery({ queryKey: ["is", start, end],
    queryFn: () => api<Income>(`/finance/statements/income?start=${start}&end=${end}`) });
  const prevYear = (d: string) => `${Number(d.slice(0, 4)) - 1}${d.slice(4)}`;
  const incomePrev = useQuery({ queryKey: ["is-prev", start, end],
    queryFn: () => api<Income>(
      `/finance/statements/income?start=${prevYear(start)}&end=${prevYear(end)}`) });
  const comprehensive = useQuery({ queryKey: ["ci", start, end],
    queryFn: () => api<Comprehensive>(`/finance/statements/comprehensive-income?start=${start}&end=${end}`) });
  const balance = useQuery({ queryKey: ["bs", end],
    queryFn: () => api<Balance>(`/finance/statements/balance-sheet?as_of=${end}`) });
  const tb = useQuery({ queryKey: ["tb", start, end],
    queryFn: () => api<TB>(`/finance/statements/trial-balance?start=${start}&end=${end}`) });
  const eqYear = Number(start.slice(0, 4));
  const eq = useQuery({ queryKey: ["eq", eqYear],
    queryFn: () => api<EquityChanges>(`/finance/statements/equity-changes?year=${eqYear}`) });
  // 운영성과표 — 의료기관 회계기준 규칙상 필수 서식인데 EP만 있고 화면이 없었다
  // (financial_ratios가 내부적으로 이미 쓰는 값이다).
  const operating = useQuery({ queryKey: ["ops", start, end],
    queryFn: () => api<Operating>(`/finance/statements/operating?start=${start}&end=${end}`) });
  const cashflow = useQuery({ queryKey: ["cf", start, end],
    queryFn: () => api<{ operating: { net_income: number; subtotal: number; adjustments: Line[] };
      investing: { subtotal: number; items: Line[] }; financing: { subtotal: number; items: Line[] };
      net_change: number; opening_cash: number; closing_cash: number; reconciled: boolean;
      unmapped_accounts: string[] }>(
      `/finance/statements/cash-flow-statement?start=${start}&end=${end}`) });
  const registry = useQuery({ queryKey: ["disc-registry", eqYear],
    queryFn: () => api<{ registry: {
      related_party_txns: { party: string; relation: string; txn_type: string; amount: number; balance: number }[];
      contingencies: { id: number; kind: string; description: string; estimate: number | null; likelihood: string; status: string }[];
      subsequent_events: { event_date: string; description: string; adjusting: boolean; reflected: boolean }[];
      accounting_changes: { change_type: string; standard: string | null; description: string; treatment: string; effect_amount: number | null }[];
    } }>(`/finance/disclosures?year=${eqYear}`) });
  const addContingency = useMutation({
    mutationFn: (v: { description: string; estimate: number }) =>
      api("/finance/contingencies", { method: "POST", body: JSON.stringify({
        period: String(eqYear), kind: "LIABILITY", description: v.description, estimate: v.estimate }) }),
    onSuccess: () => registry.refetch(),
  });
  const recognizeProvision = useMutation({
    mutationFn: (id: number) =>
      api(`/finance/contingencies/${id}/recognize-provision?on_date=${todayLocal()}`,
        { method: "POST" }),
    onSuccess: () => registry.refetch(),
  });
  const [showRpSuggest, setShowRpSuggest] = useState(false);
  const rpSuggest = useQuery({ queryKey: ["rp-suggest", eqYear], enabled: showRpSuggest,
    queryFn: () => api<{ suggestions: { party_id: number; party: string; relation: string;
      count: number; amount: number }[]; note: string }>(
      `/finance/related-parties/suggestions?year=${eqYear}`) });
  const segments = useQuery({ queryKey: ["segments", end],
    queryFn: () => api<{ segments: { dept_code: string; department: string; revenue: number;
      profit: number; assets: number; reportable: boolean }[]; total_revenue: number;
      total_profit: number; total_assets: number; reportable_count: number }>(
      `/analytics/segments?yyyymm=${end.slice(0, 7)}`) });
  const disclosures = useQuery({ queryKey: ["disc", eqYear],
    queryFn: () => api<{ auto_notes: { note_key: string; title: string; payload: Record<string, unknown> }[] }>(
      `/finance/disclosures?year=${eqYear}`) });
  const [quarter, setQuarter] = useState(1);
  const interim = useQuery({ queryKey: ["interim", eqYear, quarter],
    queryFn: () => api<{ quarterly: Agg; cumulative: Agg; prior_quarter: Agg }>(
      `/finance/statements/interim?year=${eqYear}&quarter=${quarter}`) });
  const compBs = useQuery({ queryKey: ["comp-bs", end],
    queryFn: () => api<{ current: { total_assets: number; total_liabilities: number; equity: number };
      prior: { total_assets: number; total_liabilities: number; equity: number };
      delta: { total_assets: number; total_liabilities: number; equity: number } }>(
      `/finance/statements/comparative-balance-sheet?as_of=${end}`) });
  const cfDirect = useQuery({ queryKey: ["cf-direct", start, end],
    queryFn: () => api<{ operating: Record<string, number>; operating_net: number;
      investing_net: number; financing_net: number; net_change: number; reconciled: boolean }>(
      `/finance/statements/cash-flow-direct?start=${start}&end=${end}`) });
  const ratios = useQuery({ queryKey: ["ratios", eqYear],
    queryFn: () => api<{ current_ratio: number | null; debt_ratio: number | null;
      equity_ratio: number | null; operating_margin: number | null;
      labor_cost_ratio: number | null; material_cost_ratio: number | null;
      // 비율만으론 검산도 소명도 불가 — 서버가 분자·분모를 함께 준다
      basis: { current_assets: number; current_liabilities: number; total_assets: number;
        total_liabilities: number; equity: number; revenue: number; net_income: number;
        medical_income: number; labor: number; material: number } }>(
      `/finance/statements/financial-ratios?year=${eqYear}`) });
  // 비율의 분자·분모 — 서버 basis를 그대로 병기해 검산·소명이 가능하게 한다
  const rb = ratios.data?.basis;
  const basisOf = (num?: number, den?: number) =>
    rb ? `${won(num)} / ${won(den)}` : "—";
  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : `${(v * 100).toFixed(1)}%`;

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[income, balance, comprehensive, tb, cashflow, ratios]} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재무 — 결산·재무제표</h1>
        <div className="flex items-center gap-2 text-sm">
          <input type="date" value={start} onChange={(e) => setStart(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1" />
          <span>~</span>
          <input type="date" value={end} onChange={(e) => setEnd(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1" />
          <button onClick={() => apiDownload(
            `/finance/statements/trial-balance.xlsx?start=${start}&end=${end}`, `시산표-${start}.xlsx`)}
            className="rounded-md bg-emerald-600 px-3 py-1.5 font-medium text-white hover:bg-emerald-700">
            시산표 Excel
          </button>
        </div>
      </div>

      {(income.isError || balance.isError || tb.isError) && (
        <p className="text-sm text-red-600">재무제표를 불러오지 못했습니다 (재무 권한 확인)</p>)}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`손익계산서 (${start} ~ ${end})`}>
          <Table headers={["구분", "계정", "금액"]}
            rows={[
              ...(income.data?.revenues ?? []).map((r) => ["수익", r.name ?? r.account_code, won(r.amount)]),
              ["수익 합계", "", won(income.data?.total_revenue ?? 0)],
              ...(income.data?.expenses ?? []).map((e) => ["비용", e.name ?? e.account_code, won(e.amount)]),
              ["비용 합계", "", won(income.data?.total_expense ?? 0)],
            ]} />
          <p className="mt-2 text-right text-base font-bold">
            당기순이익 <span className={(income.data?.net_income ?? 0) >= 0 ? "text-blue-600" : "text-red-600"}>
              {won(income.data?.net_income ?? 0)}</span>
          </p>
          {income.data?.by_function && (
            <div className="mt-2 border-t border-slate-100 pt-2">
              <p className="mb-1 text-xs font-semibold text-slate-500">기능별 (K-IFRS 1001)</p>
              <Table headers={["구분", "금액"]}
                rows={[
                  ["매출원가", won(income.data.by_function.cogs)],
                  ["매출총이익", won(income.data.by_function.gross_profit)],
                  ["판매관리비", won(income.data.by_function.sga)],
                  ["영업이익", won(income.data.by_function.operating_income)],
                  ["기타(영업외)", won(income.data.by_function.other)],
                ]} />
            </div>
          )}
        </Card>

        <Card title={`포괄손익계산서 (${start} ~ ${end})`}>
          <Table headers={["구분", "항목", "금액"]}
            rows={[
              ["당기손익", "당기순이익", won(comprehensive.data?.net_income ?? 0)],
              ...(comprehensive.data?.oci_items ?? []).map((o) => ["기타포괄손익(OCI)", o.name, won(o.amount)]),
              ["OCI 합계", "", won(comprehensive.data?.oci_total ?? 0)],
            ]} empty={comprehensive.isFetching ? "불러오는 중…" : "—"} />
          <p className="mt-2 text-right text-base font-bold">
            총포괄손익 <span className={(comprehensive.data?.total_comprehensive_income ?? 0) >= 0 ? "text-blue-600" : "text-red-600"}>
              {won(comprehensive.data?.total_comprehensive_income ?? 0)}</span>
          </p>
          <p className="mt-1 text-[11px] text-slate-400">OCI = 재평가잉여금·보험수리적손익 등 기간 순증감(미재분류).</p>
        </Card>

        <Card title={`재무상태표 (${end} 기준)`}>
          <Table headers={["구분", "계정", "금액"]}
            rows={[
              ...(balance.data?.assets ?? []).map((a) => ["자산", a.name ?? a.account_code, won(a.amount)]),
              ["자산 총계", "", won(balance.data?.total_assets ?? 0)],
              ...(balance.data?.liabilities ?? []).map((l) => ["부채", l.name ?? l.account_code, won(l.amount)]),
              ["부채 총계", "", won(balance.data?.total_liabilities ?? 0)],
              ["자본(이익잉여금)", "", won(balance.data?.equity ?? 0)],
            ]} />
          <p className="mt-2 text-right text-sm">
            대차 {balance.data?.balanced
              ? <Badge tone="green">일치</Badge> : <Badge tone="red">불일치</Badge>}
          </p>
          {/* 미분류 계정이 있으면 "일치"여도 신뢰할 수 없다 — 양변에서 함께 빠졌을 뿐이다 */}
          {(balance.data?.unmapped_accounts?.length ?? 0) > 0 && (
            <p className="mt-1 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">
              ⚠ 분류 미지정 계정 {balance.data!.unmapped_accounts.length}건 —
              양변에서 함께 탈락해 <b>대차가 거짓으로 맞아 보일 수 있습니다</b>:{" "}
              <span className="font-mono">{balance.data!.unmapped_accounts.join(", ")}</span>
            </p>
          )}
        </Card>
      </div>

      {/* 의료기관 회계기준 운영성과표 — 의료수익−의료비용=의료이익 → 의료외손익 → 순이익 */}
      <Card title={`운영성과표 (${start} ~ ${end}) — 의료기관 회계기준`} action={
        <span className="text-[11px] text-slate-400">{operating.data?.note}</span>}>
        <Table headers={["구분", "금액"]}
          rows={[
            ["의료수익", won(operating.data?.medical_revenue)],
            ["(−) 의료비용", won(operating.data?.medical_expense)],
            ["= 의료이익", <b key="mi">{won(operating.data?.medical_income)}</b>],
            ["(+) 의료외수익", won(operating.data?.non_medical_revenue)],
            ["(−) 의료외비용", won(operating.data?.non_medical_expense)],
            ["= 법인세차감전순이익", <b key="pt">{won(operating.data?.pretax_income)}</b>],
            ["(−) 법인세비용", won(operating.data?.income_tax)],
            ["= 당기순이익", <b key="ni">{won(operating.data?.net_income)}</b>],
          ]} />
        <div className="mt-3 grid gap-3 lg:grid-cols-2">
          <Table headers={["의료수익 계정", "금액"]}
            rows={(operating.data?.items.medical_revenue ?? []).map((a) => [
              `${a.account_code} ${a.name}`, won(a.amount)])} empty="계정 없음" />
          <Table headers={["의료비용 계정", "금액"]}
            rows={(operating.data?.items.medical_expense ?? []).map((a) => [
              `${a.account_code} ${a.name}`, won(a.amount)])} empty="계정 없음" />
        </div>
      </Card>

      <Card title={`손익 전년 동기 비교 (${start.slice(0, 4)} vs ${Number(start.slice(0, 4)) - 1})`}>
        <Table headers={["구분", "당기", "전년 동기", "증감", "증감률"]}
          rows={([
            ["수익", income.data?.total_revenue, incomePrev.data?.total_revenue],
            ["비용", income.data?.total_expense, incomePrev.data?.total_expense],
            ["당기순이익", income.data?.net_income, incomePrev.data?.net_income],
          ] as [string, number | undefined, number | undefined][]).map(([k, cur, prv]) => {
            const c = cur ?? 0, p = prv ?? 0, diff = c - p;
            const rate = p ? `${((diff / Math.abs(p)) * 100).toFixed(1)}%` : "—";
            return [k, won(c), won(p),
              <span key="d" className={diff >= 0 ? "text-blue-600" : "text-red-600"}>
                {diff >= 0 ? "+" : ""}{won(diff)}</span>, rate];
          })} />
      </Card>

      <Card title={`시산표 (${start} ~ ${end}) — 복식부기 검증`}>
        <Table headers={["계정", "구분", "차변", "대변"]}
          rows={(tb.data?.accounts ?? []).map((a) => [
            a.name ?? a.account_code, a.kind, won(a.debit), won(a.credit)])} />
        <p className="mt-2 text-right text-sm">
          총차변 {won(tb.data?.total_debit ?? 0)} / 총대변 {won(tb.data?.total_credit ?? 0)}{" "}
          {tb.data?.balanced ? <Badge tone="green">일치</Badge> : <Badge tone="red">불일치</Badge>}
        </p>
      </Card>

      <Card title={`기본금변동계산서 (${eqYear}년 — 의료기관 회계기준)`} action={
        <button onClick={() => apiDownload(
          `/finance/statements/equity-changes.xlsx?year=${eqYear}`,
          `기본금변동계산서-${eqYear}.xlsx`)}
          className="rounded-md bg-slate-600 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700">
          Excel</button>}>
        <Table headers={["과목", "기초잔액", "증가", "감소", "기말잔액"]}
          rows={(eq.data?.items ?? []).map((i) => [
            i.name, won(i.opening), won(i.increase), won(i.decrease), won(i.closing)])} />
        <p className="mt-2 text-right text-sm">
          자본총계 {won(eq.data?.total_opening ?? 0)} → {won(eq.data?.total_closing ?? 0)}
          {" "}(당기순이익 {won(eq.data?.net_income ?? 0)}){" "}
          {eq.data?.balanced ? <Badge tone="green">재무상태표 정합</Badge>
            : <Badge tone="red">정합 불일치</Badge>}
        </p>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`현금흐름표 (${start} ~ ${end}) — 간접법`}>
          <Table headers={["활동", "금액"]}
            rows={[
              // 간접법 출발점(당기순이익)이 없으면 조정액 합을 검산할 수 없다
              ["당기순이익 (간접법 출발점)", won(cashflow.data?.operating.net_income ?? 0)],
              ...(cashflow.data?.operating.adjustments ?? []).map((a) => [
                `  조정 · ${a.name ?? a.account_code}`, won(a.amount)]),
              ["영업활동 소계", won(cashflow.data?.operating.subtotal ?? 0)],
              ...(cashflow.data?.investing.items ?? []).map((a) => [
                `  투자 · ${a.name ?? a.account_code}`, won(a.amount)]),
              ["투자활동 소계", won(cashflow.data?.investing.subtotal ?? 0)],
              ...(cashflow.data?.financing.items ?? []).map((a) => [
                `  재무 · ${a.name ?? a.account_code}`, won(a.amount)]),
              ["재무활동 소계", won(cashflow.data?.financing.subtotal ?? 0)],
              ["현금 순증감", won(cashflow.data?.net_change ?? 0)],
              ["기초 → 기말현금",
                `${won(cashflow.data?.opening_cash ?? 0)} → ${won(cashflow.data?.closing_cash ?? 0)}`],
            ]} />
          <p className="mt-2 text-right text-sm">
            {cashflow.data?.reconciled
              ? <Badge tone="green">현금 정합</Badge> : <Badge tone="red">정합 불일치</Badge>}
          </p>
          {/* 활동 미분류 계정은 영업활동으로 폴백된다 → 영업활동 과대계상의 원인 */}
          {(cashflow.data?.unmapped_accounts?.length ?? 0) > 0 && (
            <p className="mt-1 rounded-md bg-amber-50 px-2 py-1 text-xs text-amber-800">
              ⚠ 활동 미분류 {cashflow.data!.unmapped_accounts.length}건 — 영업활동으로 폴백돼
              과대계상될 수 있습니다:{" "}
              <span className="font-mono">{cashflow.data!.unmapped_accounts.join(", ")}</span>
            </p>
          )}
          <div className="mt-2 border-t border-slate-100 pt-2">
            <p className="mb-1 text-xs font-semibold text-slate-500">직접법 (K-IFRS 1007 병기)</p>
            <Table headers={["영업활동", "금액"]}
              rows={[
                ["고객 수취", won(cfDirect.data?.operating["고객수취"] ?? 0)],
                ["종업원 지급", won(cfDirect.data?.operating["종업원지급"] ?? 0)],
                ["공급자·기타 지급", won(cfDirect.data?.operating["공급자기타지급"] ?? 0)],
                ["영업활동 순현금", won(cfDirect.data?.operating_net ?? 0)],
                ["투자/재무활동", `${won(cfDirect.data?.investing_net ?? 0)} / ${won(cfDirect.data?.financing_net ?? 0)}`],
              ]} />
            {cfDirect.data && <p className="mt-1 text-right text-xs">{cfDirect.data.reconciled ? <Badge tone="green">정합</Badge> : <Badge tone="red">불일치</Badge>}</p>}
          </div>
        </Card>

        <Card title={`재무비율·경영 KPI (${eqYear}년)`}>
          {/* 비율만으론 검산도 소명도 불가 — 서버가 분자·분모를 함께 주는데 버리고 있었다.
              특히 '의료수익의료이익률'의 분자는 순이익이 아니라 **의료이익**이라 병기가 필요하다. */}
          <Table headers={["지표", "값", "산출 근거(분자 / 분모)"]}
            rows={[
              ["유동비율", pct(ratios.data?.current_ratio),
               basisOf(rb?.current_assets, rb?.current_liabilities)],
              ["부채비율", pct(ratios.data?.debt_ratio),
               basisOf(rb?.total_liabilities, rb?.total_assets)],
              ["자기자본비율", pct(ratios.data?.equity_ratio), basisOf(rb?.equity, rb?.total_assets)],
              ["의료수익의료이익률", pct(ratios.data?.operating_margin),
               basisOf(rb?.medical_income, rb?.revenue)],
              ["인건비율", pct(ratios.data?.labor_cost_ratio), basisOf(rb?.labor, rb?.revenue)],
              ["재료비율", pct(ratios.data?.material_cost_ratio), basisOf(rb?.material, rb?.revenue)],
            ]} />
        </Card>
      </div>

      <Card title={`공시 등록 (${eqYear}년 — 특수관계자·우발·후속사건)`} action={
        <button onClick={() => {
          const d = window.prompt("우발부채 내용 (K-IFRS 1037)");
          if (!d) return;
          const e = window.prompt("추정액(원, 없으면 0)", "0");
          addContingency.mutate({ description: d, estimate: Number(e || 0) });
        }} className="text-xs font-medium text-blue-600 hover:underline">우발부채 추가</button>}>
        <div className="space-y-3 text-sm">
          <div>
            <p className="mb-1 font-semibold text-slate-600">특수관계자 거래 (K-IFRS 1024)</p>
            <Table headers={["특수관계자", "관계", "거래유형", "금액", "잔액"]}
              rows={(registry.data?.registry.related_party_txns ?? []).map((t) => [
                t.party, t.relation, t.txn_type, won(t.amount), won(t.balance)])} empty="등록 없음" />
            <div className="mt-1">
              <button onClick={() => setShowRpSuggest(true)}
                className="text-xs text-blue-600 hover:underline">자동 탐지 (전표 적요 매칭)</button>
              {showRpSuggest && (
                <div className="mt-1">
                  <Table headers={["특수관계자", "관계", "매칭건수", "추정금액(차변합)"]}
                    rows={(rpSuggest.data?.suggestions ?? []).map((s) => [
                      s.party, s.relation, s.count, won(s.amount)])}
                    empty={rpSuggest.isFetching ? "탐지 중…" : "매칭 전표 없음"} />
                  <p className="mt-0.5 text-[11px] text-slate-400">
                    {rpSuggest.data?.note ?? "적요 이름매칭 보조 — 확인 후 거래 등록(오탐 가능)"}</p>
                </div>
              )}
            </div>
          </div>
          <div>
            <p className="mb-1 font-semibold text-slate-600">우발부채·우발자산 (K-IFRS 1037)</p>
            <Table headers={["구분", "내용", "추정액", "가능성", "상태", ""]}
              rows={(registry.data?.registry.contingencies ?? []).map((c) => [
                c.kind === "LIABILITY" ? "우발부채" : "우발자산", c.description,
                c.estimate == null ? "—" : won(c.estimate), c.likelihood, c.status,
                c.kind === "LIABILITY" && c.status === "OPEN" && c.estimate != null
                  ? <button key="p" disabled={recognizeProvision.isPending} onClick={() => recognizeProvision.mutate(c.id)}
                      title="확률 높음·추정가능 시 충당부채로 인식(5680/2130)"
                      className="rounded bg-amber-600 px-2 py-0.5 text-xs text-white disabled:opacity-50">충당전환</button>
                  : "",
              ])} empty="등록 없음" />
          </div>
          <div>
            <p className="mb-1 font-semibold text-slate-600">보고기간후사건 (K-IFRS 1010)</p>
            <Table headers={["일자", "내용", "유형", "반영"]}
              rows={(registry.data?.registry.subsequent_events ?? []).map((e) => [
                e.event_date, e.description, e.adjusting ? "수정" : "비수정",
                e.adjusting ? (e.reflected ? "반영" : "미반영") : "주석"])} empty="등록 없음" />
          </div>
          <div>
            <p className="mb-1 font-semibold text-slate-600">회계정책·추정변경·오류 (K-IFRS 1008)</p>
            <Table headers={["유형", "기준서", "내용", "처리", "영향액"]}
              rows={(registry.data?.registry.accounting_changes ?? []).map((c) => [
                c.change_type === "POLICY" ? "정책변경" : c.change_type === "ESTIMATE" ? "추정변경" : "오류수정",
                c.standard ?? "—", c.description,
                c.treatment === "RETROSPECTIVE" ? "소급" : "전진",
                c.effect_amount == null ? "—" : won(c.effect_amount)])} empty="등록 없음" />
          </div>
        </div>
      </Card>

      <Card title={`영업부문 보고 (${end.slice(0, 7)} — K-IFRS 1108)`} action={
        <span className="text-[11px] text-slate-400">보고부문(10% 임계) {segments.data?.reportable_count ?? 0}개 · 진료과=부문</span>}>
        <Table headers={["부문(진료과)", "수익", "손익", "자산", "보고부문"]}
          rows={(segments.data?.segments ?? []).map((s) => [
            s.department ?? s.dept_code, won(s.revenue),
            <span key="p" className={s.profit < 0 ? "text-red-600" : ""}>{won(s.profit)}</span>,
            won(s.assets), s.reportable ? <Badge key="r" tone="green">보고</Badge> : <span key="r" className="text-slate-400">기타</span>,
          ])} empty={segments.isFetching ? "불러오는 중…" : "부문 데이터 없음"} />
        <p className="mt-2 text-right text-sm">총 수익 {won(segments.data?.total_revenue ?? 0)} · 손익 {won(segments.data?.total_profit ?? 0)} · 자산 {won(segments.data?.total_assets ?? 0)}</p>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`중간재무보고 (${eqYear}년 — K-IFRS 1034)`} action={
          <select value={quarter} onChange={(e) => setQuarter(Number(e.target.value))}
            className="rounded border border-slate-300 px-2 py-0.5 text-xs">
            {[1, 2, 3, 4].map((q) => <option key={q} value={q}>{q}분기</option>)}</select>}>
          <Table headers={["구분", "수익", "비용", "순이익"]}
            rows={[
              ["당분기", won(interim.data?.quarterly.revenue ?? 0), won(interim.data?.quarterly.expense ?? 0), won(interim.data?.quarterly.net ?? 0)],
              ["누적(YTD)", won(interim.data?.cumulative.revenue ?? 0), won(interim.data?.cumulative.expense ?? 0), won(interim.data?.cumulative.net ?? 0)],
              ["전년 동분기", won(interim.data?.prior_quarter.revenue ?? 0), won(interim.data?.prior_quarter.expense ?? 0), won(interim.data?.prior_quarter.net ?? 0)],
            ]} />
        </Card>

        <Card title={`전기 비교 재무상태표 (${end} — K-IFRS 1001)`}>
          <Table headers={["구분", "당기말", "전기말", "증감"]}
            rows={[
              ["자산총계", won(compBs.data?.current.total_assets ?? 0), won(compBs.data?.prior.total_assets ?? 0), won(compBs.data?.delta.total_assets ?? 0)],
              ["부채총계", won(compBs.data?.current.total_liabilities ?? 0), won(compBs.data?.prior.total_liabilities ?? 0), won(compBs.data?.delta.total_liabilities ?? 0)],
              ["자본", won(compBs.data?.current.equity ?? 0), won(compBs.data?.prior.equity ?? 0), won(compBs.data?.delta.equity ?? 0)],
            ]} />
        </Card>
      </div>

      <Card title={`재무제표 주석 (${eqYear}년 — K-IFRS 1001, 자동 정량주석)`}>
        {(() => {
          const ppe = disclosures.data?.auto_notes.find((n) => n.note_key === "ppe_movement")?.payload as
            { cost: Record<string, number>; accum_dep: Record<string, number>; book_value: Record<string, number> } | undefined;
          const prov = disclosures.data?.auto_notes.find((n) => n.note_key === "provision_movement")?.payload as
            { items: { account_code: string; name: string; opening: number; charge: number; release: number; closing: number }[] } | undefined;
          const lease = disclosures.data?.auto_notes.find((n) => n.note_key === "lease_maturity")?.payload as
            { buckets: { bucket: string; undiscounted: number }[]; present_value_total: number } | undefined;
          const fv = disclosures.data?.auto_notes.find((n) => n.note_key === "fair_value")?.payload as
            { levels: { level: string; book_value: number; count: number }[]; total: number } | undefined;
          return (
            <div className="space-y-4">
              <div>
                <p className="mb-1 text-sm font-semibold text-slate-600">유형자산 증감 (K-IFRS 1016)</p>
                <Table headers={["구분", "기초", "취득", "처분/감소", "기말"]}
                  rows={ppe ? [
                    ["취득원가", won(ppe.cost.opening), won(ppe.cost.acquisition), won(ppe.cost.disposal), won(ppe.cost.closing)],
                    ["감가상각누계", won(ppe.accum_dep.opening), won(ppe.accum_dep.charge), won(ppe.accum_dep.disposal), won(ppe.accum_dep.closing)],
                    ["장부가액", won(ppe.book_value.opening), "", "", won(ppe.book_value.closing)],
                  ] : []} empty={disclosures.isFetching ? "불러오는 중…" : "—"} />
              </div>
              <div>
                <p className="mb-1 text-sm font-semibold text-slate-600">충당부채·충당금 증감 (K-IFRS 1037)</p>
                <Table headers={["계정", "기초", "전입", "감소", "기말"]}
                  rows={(prov?.items ?? []).map((i) => [i.name, won(i.opening), won(i.charge), won(i.release), won(i.closing)])}
                  empty="—" />
              </div>
              <div>
                <p className="mb-1 text-sm font-semibold text-slate-600">리스부채 만기분석 (K-IFRS 1116)</p>
                <Table headers={["만기", "미할인 지급액"]}
                  rows={(lease?.buckets ?? []).map((b) => [b.bucket, won(b.undiscounted)])}
                  empty="리스 없음" />
                {lease && <p className="mt-1 text-right text-xs text-slate-500">현재가치 합 {won(lease.present_value_total)}</p>}
              </div>
              <div>
                <p className="mb-1 text-sm font-semibold text-slate-600">공정가치 서열 (K-IFRS 1113)</p>
                <Table headers={["수준", "장부가", "건수"]}
                  rows={(fv?.levels ?? []).map((l) => [`Level ${l.level}`, won(l.book_value), `${l.count}건`])}
                  empty="재평가 자산 없음" />
              </div>
              <p className="text-[11px] text-slate-400">정성 주석(회계정책·특수관계자 등)은 PUT /finance/disclosures로 편집·저장.</p>
            </div>
          );
        })()}
      </Card>
    </div>
  );
}
