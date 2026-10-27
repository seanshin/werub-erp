"use client";
/** 재무 미래 시뮬레이션 (MVP) — 드라이버 슬라이더로 향후 12개월 손익·BEP 즉시 투영. */
import { monthLocal } from "@/lib/dates";
import { useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bar, CartesianGrid, ComposedChart, Legend, Line,
  ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { Baseline, Drivers, baseDrivers, project } from "@/lib/sim";
import { Card, Table } from "@/components/ui";
import { won as fmtWon } from "@/lib/format";

const man = (n: number) => `${Math.round(n / 10000).toLocaleString()}만`;
const won = (n: number) => fmtWon(Math.round(n));
interface Scenario { id: number; name: string; drivers: Drivers }
interface DeptBase { dept_code: string; department: string | null;
  avg_revenue: number; avg_cost: number; avg_deduction: number; avg_profit: number; margin: number | null }
const annualNetOf = (b: Baseline, dr: Drivers) => project(b, dr).reduce((a, r) => a + r.순이익, 0);

// 목표 연 순이익 달성에 필요한 매출 성장률(이진탐색, 단조 증가)
function solveGrowth(b: Baseline, d: Drivers, target: number): number | null {
  let lo = -0.3, hi = 1.0;
  if (annualNetOf(b, { ...d, growth: hi }) < target) return null;   // 도달 불가
  if (annualNetOf(b, { ...d, growth: lo }) > target) return lo;
  for (let i = 0; i < 40; i++) {
    const mid = (lo + hi) / 2;
    if (annualNetOf(b, { ...d, growth: mid }) < target) lo = mid; else hi = mid;
  }
  const res = (lo + hi) / 2;
  // 단조성 가정이 깨진 구간(세율 꺾임 등)에서 잘못된 해 방지 — 수렴값이 목표 근처인지 검증
  const tol = Math.max(Math.abs(target) * 0.01, 1_000_000);
  return Math.abs(annualNetOf(b, { ...d, growth: res }) - target) <= tol ? res : null;
}

function exportCsv(rows: { label: string; 수익: number; 순이익: number; 현금잔액: number }[]) {
  const head = "월,수익,순이익,현금잔액\n";
  const body = rows.map((r) => `${r.label},${r.수익},${r.순이익},${r.현금잔액}`).join("\n");
  const url = URL.createObjectURL(new Blob(["﻿" + head + body], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url; a.download = "finance-simulation.csv"; a.click();
  URL.revokeObjectURL(url);
}

// 의존성 없는 PDF — 새 창에 보고서 HTML(차트 SVG 직렬화) 작성 후 인쇄(사용자가 PDF로 저장)
function printReport(p: {
  title: string; subtitle: string;
  kpis: { label: string; value: string }[];
  charts: { title: string; svg: string }[];
  table?: { title: string; headers: string[]; rows: string[][] };
}) {
  const w = window.open("", "_blank", "width=960,height=1200");
  if (!w) { window.alert("팝업이 차단되었습니다. 허용 후 다시 시도해 주세요."); return; }
  const esc = (s: string) => s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]!));
  const kpiHtml = p.kpis.map((k) =>
    `<div class="kpi"><div class="kl">${esc(k.label)}</div><div class="kv">${esc(k.value)}</div></div>`).join("");
  const chartHtml = p.charts.filter((c) => c.svg).map((c) =>
    `<section><h2>${esc(c.title)}</h2><div class="chart">${c.svg}</div></section>`).join("");
  const tableHtml = p.table ? `<section><h2>${esc(p.table.title)}</h2><table><thead><tr>${
    p.table.headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${
    p.table.rows.map((r) => `<tr>${r.map((c) => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")
  }</tbody></table></section>` : "";
  w.document.write(`<!doctype html><html lang="ko"><head><meta charset="utf-8"><title>${esc(p.title)}</title><style>
    *{box-sizing:border-box}body{font-family:-apple-system,'Segoe UI',sans-serif;color:#0f172a;padding:28px;margin:0}
    h1{font-size:18px;margin:0 0 4px}.sub{color:#64748b;font-size:12px;margin:0 0 16px}
    .kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px}
    .kpi{border:1px solid #e2e8f0;border-radius:8px;padding:8px}.kl{font-size:11px;color:#64748b}.kv{font-size:15px;font-weight:700}
    section{margin-bottom:18px;page-break-inside:avoid}h2{font-size:13px;margin:0 0 6px}
    .chart svg{max-width:100%;height:auto}table{width:100%;border-collapse:collapse;font-size:12px}
    th,td{border:1px solid #e2e8f0;padding:4px 8px;text-align:right}th:first-child,td:first-child{text-align:left}th{background:#f8fafc}
    @media print{body{padding:0}}
  </style></head><body><h1>${esc(p.title)}</h1><p class="sub">${esc(p.subtitle)}</p>
  <div class="kpis">${kpiHtml}</div>${chartHtml}${tableHtml}</body></html>`);
  w.document.close(); w.focus();
  w.setTimeout(() => w.print(), 400);
}

const OVERLAY_COLORS = ["#0f172a", "#ef4444", "#f59e0b", "#8b5cf6", "#10b981"];

function Slider({ label, value, min, max, step, fmt, onChange }: {
  label: string; value: number; min: number; max: number; step: number;
  fmt: (v: number) => string; onChange: (v: number) => void;
}) {
  return (
    <label className="block text-sm">
      <span className="flex justify-between"><span className="text-slate-600">{label}</span>
        <b className="text-blue-600">{fmt(value)}</b></span>
      <input type="range" min={min} max={max} step={step} value={value}
        onChange={(e) => onChange(Number(e.target.value))} className="w-full" />
    </label>
  );
}

export default function SimulationPage() {
  const [ym] = useState(monthLocal());
  const base = useQuery({ queryKey: ["sim-baseline", ym],
    queryFn: () => api<Baseline>(`/analytics/sim-baseline?yyyymm=${ym}`) });
  const b = base.data;
  const [d, setD] = useState<Drivers>({ growth: 0, varRatio: 0, fixedDelta: 0, taxRate: 0, capex: 0, loan: 0, seasonal: false });
  const [initd, setInit] = useState(false);
  const [target, setTarget] = useState("");                  // goal-seek 목표 연 순이익(만원)
  const [cmp, setCmp] = useState<number[]>([]);              // 오버레이 비교 대상 시나리오 id
  const pnlRef = useRef<HTMLDivElement>(null);
  const cashRef = useRef<HTMLDivElement>(null);
  const qc = useQueryClient();
  const scnQuery = useQuery({ queryKey: ["sim-scenarios"],
    queryFn: () => api<{ items: Scenario[] }>("/analytics/sim-scenarios") });
  const scenarios = useMemo(() => scnQuery.data?.items ?? [], [scnQuery.data]);
  const deptQuery = useQuery({ queryKey: ["sim-dept-baseline", ym],
    queryFn: () => api<{ departments: DeptBase[] }>(`/analytics/sim-dept-baseline?yyyymm=${ym}`) });
  // baseline 로드 시 1회 추천값 반영
  if (b && !initd) { setD(baseDrivers(b)); setInit(true); }

  const rows = useMemo(() => b ? project(b, d) : [], [b, d]);
  const baseRows = useMemo(() => b ? project(b, baseDrivers(b)) : [], [b]);
  const merged = rows.map((r, i) => ({ label: r.label, 시나리오순이익: r.순이익, 기준순이익: baseRows[i]?.순이익 ?? 0, 수익: r.수익 }));
  const annualNet = rows.reduce((a, r) => a + r.순이익, 0);
  const annualRev = rows.reduce((a, r) => a + r.수익, 0);
  const margin = annualRev ? (annualNet / annualRev * 100).toFixed(1) : "—";
  // BEP (월) — 고정비/(1-변동비율), 안전한계율
  const fixedM = b ? b.monthly.fixed * (1 + d.fixedDelta) + b.monthly.other : 0;
  const bep = b && d.varRatio < 1 ? fixedM / (1 - d.varRatio) : 0;
  const curRev = rows[0]?.수익 ?? 0;
  const safety = curRev ? ((curRev - bep) / curRev * 100).toFixed(1) : "—";
  // 현금 — 최저잔액·부족 시점
  const minCash = rows.length ? Math.min(...rows.map((r) => r.현금잔액)) : 0;
  const shortage = rows.find((r) => r.현금잔액 < 0)?.label;
  // 민감도(토네이도) — 드라이버 ±변동 시 연 순이익 영향
  const annOf = (dr: Drivers) => b ? project(b, dr).reduce((a, r) => a + r.순이익, 0) : 0;
  const tornado = b ? [
    { k: "매출 성장률 ±1%p", lo: annOf({ ...d, growth: d.growth - 0.01 }), hi: annOf({ ...d, growth: d.growth + 0.01 }) },
    { k: "변동비율 ±2%p", lo: annOf({ ...d, varRatio: d.varRatio + 0.02 }), hi: annOf({ ...d, varRatio: d.varRatio - 0.02 }) },
    { k: "고정비 ±5%", lo: annOf({ ...d, fixedDelta: d.fixedDelta + 0.05 }), hi: annOf({ ...d, fixedDelta: d.fixedDelta - 0.05 }) },
  ].map((t) => ({ ...t, range: Math.abs(t.hi - t.lo) })).sort((a, b2) => b2.range - a.range) : [];
  // 진료과별 투영 — 성장률(g) 12개월 복리 누적배수, 원가는 고정(고정비증감 반영)
  const deptRows = useMemo(() => {
    const depts = deptQuery.data?.departments ?? [];
    let mult = 0; for (let m = 1; m <= 12; m++) mult += Math.pow(1 + d.growth, m);
    return depts.map((x) => {
      const annualRev = x.avg_revenue * mult;
      const annualDed = x.avg_deduction * mult;                 // 삭감은 활동(수익)에 비례
      const annualCost = x.avg_cost * 12 * (1 + d.fixedDelta);  // 배부원가는 고정성
      const annualProfit = annualRev - annualCost - annualDed;
      return { name: x.department || x.dept_code, 예상연수익: Math.round(annualRev),
        예상연손익: Math.round(annualProfit),
        손익률: annualRev ? +(annualProfit / annualRev * 100).toFixed(1) : 0 };
    });
  }, [deptQuery.data, d.growth, d.fixedDelta]);

  // 시나리오 오버레이 비교 — 현재 + 선택 시나리오들의 월별 순이익을 한 차트에
  const overlay = useMemo(() => {
    if (!b) return [] as Record<string, number | string>[];
    const sel = scenarios.filter((s) => cmp.includes(s.id));
    const series = sel.map((s) => project(b, s.drivers));
    return rows.map((r, i) => {
      const o: Record<string, number | string> = { label: r.label, 현재: r.순이익 };
      sel.forEach((s, si) => { o[s.name] = series[si][i]?.순이익 ?? 0; });
      return o;
    });
  }, [b, rows, scenarios, cmp]);
  const cmpNames = scenarios.filter((s) => cmp.includes(s.id)).map((s) => s.name);

  // 계절지수가 의미있게 변동하는지(전년 데이터 존재) — 모두 1.0이면 토글 비활성
  const hasSeason = !!b?.seasonality?.some((f) => Math.abs(f - 1) > 0.01);
  const cv = b?.revenue_cv ?? 0;
  const conf = cv < 0.15 ? { t: "높음", c: "text-emerald-600" } : cv < 0.35 ? { t: "보통", c: "text-amber-600" } : { t: "낮음", c: "text-red-600" };
  // 목표 역산(goal-seek)
  const targetNet = Number(target) * 10000;
  const reqGrowth = b && target && !Number.isNaN(targetNet) ? solveGrowth(b, d, targetNet) : undefined;

  const saveMut = useMutation<Scenario, Error, { name: string; drivers: Drivers }>({
    mutationFn: (v) => api<Scenario>("/analytics/sim-scenarios",
      { method: "POST", body: JSON.stringify(v) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sim-scenarios"] }),
  });
  const delMut = useMutation<unknown, Error, number>({
    mutationFn: (id) => api(`/analytics/sim-scenarios/${id}`, { method: "DELETE" }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sim-scenarios"] }),
  });
  const saveScenario = () => {
    const name = window.prompt("시나리오 이름", `시나리오 ${scenarios.length + 1}`);
    if (name?.trim()) saveMut.mutate({ name: name.trim(), drivers: d });
  };
  const exportPdf = () => {
    if (!b) return;
    printReport({
      title: "재무 미래 시뮬레이션",
      subtitle: `기준 ${ym} · 최근 ${b.months_window}개월 기준선 · 성장률 ${(d.growth * 100).toFixed(1)}%`
        + `·변동비율 ${(d.varRatio * 100).toFixed(0)}%${d.seasonal ? "·계절성 적용" : ""}`,
      kpis: [
        { label: "연 순이익(예측)", value: man(annualNet) + "원" },
        { label: "연 매출(예측)", value: man(annualRev) + "원" },
        { label: "순이익률", value: `${margin}%` },
        { label: "손익분기(월매출)", value: man(bep) + "원" },
      ],
      charts: [
        { title: "월별 순이익 — 기준 vs 시나리오", svg: pnlRef.current?.querySelector("svg")?.outerHTML ?? "" },
        { title: "현금흐름 예측 (12개월)", svg: cashRef.current?.querySelector("svg")?.outerHTML ?? "" },
      ],
      table: deptRows.length ? {
        title: "진료과별 예상 (연환산)",
        headers: ["진료과", "예상 연수익", "예상 연손익", "손익률"],
        rows: deptRows.map((x) => [x.name, man(x.예상연수익) + "원", man(x.예상연손익) + "원", `${x.손익률}%`]),
      } : undefined,
    });
  };

  if (base.isLoading) return <p className="p-4 text-sm text-slate-400">기준선 불러오는 중…</p>;
  if (base.isError || !b) return <p className="p-4 text-sm text-red-600">권한 또는 데이터 오류</p>;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 미래 시뮬레이션</h1>
      <p className="text-xs text-slate-400">기준선: 최근 {b.months_window}개월 월평균 (수익 {man(b.monthly.revenue)}원·
        고정비 {man(b.monthly.fixed)}원·변동비율 {(b.variable_ratio * 100).toFixed(1)}%) ·
        추세 신뢰도 <b className={conf.c}>{conf.t}</b>(매출 변동계수 {(cv * 100).toFixed(0)}%). 드라이버 조정 시 즉시 투영.</p>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <Card title="가정(드라이버)">
          <div className="space-y-3">
            <Slider label="월 매출 성장률" value={d.growth} min={-0.1} max={0.1} step={0.005}
              fmt={(v) => `${(v * 100).toFixed(1)}%`} onChange={(v) => setD({ ...d, growth: v })} />
            <Slider label="변동비율" value={d.varRatio} min={0} max={0.8} step={0.01}
              fmt={(v) => `${(v * 100).toFixed(0)}%`} onChange={(v) => setD({ ...d, varRatio: v })} />
            <Slider label="고정비 증감" value={d.fixedDelta} min={-0.3} max={0.5} step={0.01}
              fmt={(v) => `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}%`} onChange={(v) => setD({ ...d, fixedDelta: v })} />
            <Slider label="법인세율" value={d.taxRate} min={0} max={0.3} step={0.01}
              fmt={(v) => `${(v * 100).toFixed(0)}%`} onChange={(v) => setD({ ...d, taxRate: v })} />
            <Slider label="투자(장비 등, 1월)" value={d.capex} min={0} max={3_000_000_000} step={50_000_000}
              fmt={(v) => man(v) + "원"} onChange={(v) => setD({ ...d, capex: v })} />
            <Slider label="차입(1월)" value={d.loan} min={0} max={3_000_000_000} step={50_000_000}
              fmt={(v) => man(v) + "원"} onChange={(v) => setD({ ...d, loan: v })} />
            <label className="flex items-center gap-2 text-sm text-slate-600">
              <input type="checkbox" checked={d.seasonal} disabled={!hasSeason}
                onChange={(e) => setD({ ...d, seasonal: e.target.checked })} />
              계절성 적용 (전년 월별 가중)
              {!hasSeason && <span className="text-xs text-slate-400">전년 데이터 부족</span>}
            </label>
            <button onClick={() => setD(baseDrivers(b))}
              className="text-xs font-medium text-blue-600 hover:underline">기준값으로 초기화</button>
          </div>
        </Card>

        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <Kpi label="연 순이익(예측)" value={man(annualNet) + "원"} tone={annualNet >= 0 ? "text-blue-600" : "text-red-600"} />
            <Kpi label="연 매출(예측)" value={man(annualRev) + "원"} />
            <Kpi label="순이익률" value={`${margin}%`} />
            <Kpi label="손익분기(월매출)" value={man(bep) + "원"} />
          </div>
          <Card title="월별 순이익 — 기준 vs 시나리오">
            <div ref={pnlRef}>
            <ResponsiveContainer width="100%" height={260}>
              <ComposedChart data={merged} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={64} />
                <Tooltip formatter={(v) => won(Number(v))} /><Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="기준순이익" fill="#cbd5e1" radius={[3, 3, 0, 0]} />
                <Bar dataKey="시나리오순이익" fill="#3b82f6" radius={[3, 3, 0, 0]} />
                <Line dataKey="수익" stroke="#10b981" dot={false} strokeWidth={2} />
              </ComposedChart>
            </ResponsiveContainer>
            </div>
            <p className="mt-1 text-xs text-slate-400">안전한계율(첫 달 기준) {safety}% — 매출이 BEP 대비 여유.
              손익분기 = (고정비+기타)/(1−변동비율). 시뮬레이션은 읽기전용(전표 미생성).</p>
          </Card>

          <Card title="현금흐름 예측 (12개월)" action={shortage
            ? <span className="text-xs font-medium text-red-600">⚠ {shortage} 자금 부족</span>
            : <span className="text-xs text-emerald-600">자금 부족 없음</span>}>
            <div ref={cashRef}>
            <ResponsiveContainer width="100%" height={220}>
              <ComposedChart data={rows} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={64} />
                <Tooltip formatter={(v) => won(Number(v))} />
                <Line dataKey="현금잔액" stroke={shortage ? "#ef4444" : "#3b82f6"} dot={false} strokeWidth={2} />
              </ComposedChart>
            </ResponsiveContainer>
            </div>
            <p className="mt-1 text-xs text-slate-400">영업현금=순이익+감가−매출증가(운전자본)+투자/차입(1월).
              최저잔액 {man(minCash)}원. 기초현금 {man(b.cash)}원.</p>
          </Card>

          <Card title="민감도 분석 (연 순이익 영향)">
            <ResponsiveContainer width="100%" height={150}>
              <ComposedChart layout="vertical" data={tornado} margin={{ top: 4, right: 8, left: 60, bottom: 4 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                <XAxis type="number" tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 10 }} />
                <YAxis type="category" dataKey="k" tick={{ fontSize: 10 }} width={110} />
                <Tooltip formatter={(v) => won(Number(v))} />
                <Bar dataKey="range" fill="#8b5cf6" radius={[0, 3, 3, 0]} />
              </ComposedChart>
            </ResponsiveContainer>
            <p className="mt-1 text-xs text-slate-400">막대가 길수록 순이익에 민감 — 위에서부터 핵심 변수.</p>
          </Card>

          <Card title="목표 역산 (goal-seek)">
            <div className="flex flex-wrap items-end gap-2 text-sm">
              <label>목표 연 순이익(만원)
                <input value={target} onChange={(e) => setTarget(e.target.value.replace(/[^0-9-]/g, ""))}
                  placeholder="예: 50000" className="ml-2 w-32 rounded border border-slate-300 px-2 py-1" />
              </label>
              {reqGrowth === null && <span className="text-red-600">달성 불가(성장률 100%로도 미달)</span>}
              {reqGrowth != null && reqGrowth !== undefined && (
                <>
                  <span>필요 월 매출 성장률 <b className="text-blue-600">{(reqGrowth * 100).toFixed(2)}%</b></span>
                  <button onClick={() => setD({ ...d, growth: reqGrowth })}
                    className="rounded bg-blue-600 px-2 py-1 text-xs font-medium text-white">적용</button>
                </>
              )}
            </div>
            <p className="mt-1 text-xs text-slate-400">현재 가정 고정, 매출 성장률만 역산. 변동비·고정비 조정 후 재계산 가능.</p>
          </Card>

          <Card title="진료과별 시뮬레이션 (성장률·고정비 가정 적용)">
            {deptRows.length === 0
              ? <p className="text-xs text-slate-400">{deptQuery.isLoading ? "불러오는 중…" : "진료과별 데이터 없음(청구 결정·원가배부 스냅샷 필요)"}</p>
              : <>
                <ResponsiveContainer width="100%" height={Math.max(160, deptRows.length * 34)}>
                  <ComposedChart layout="vertical" data={deptRows} margin={{ top: 4, right: 12, left: 40, bottom: 4 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                    <XAxis type="number" tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 10 }} />
                    <YAxis type="category" dataKey="name" tick={{ fontSize: 11 }} width={84} />
                    <Tooltip formatter={(v) => won(Number(v))} /><Legend wrapperStyle={{ fontSize: 11 }} />
                    <Bar dataKey="예상연수익" fill="#3b82f6" radius={[0, 3, 3, 0]} />
                    <Bar dataKey="예상연손익" fill="#10b981" radius={[0, 3, 3, 0]} />
                  </ComposedChart>
                </ResponsiveContainer>
                <Table headers={["진료과", "예상 연수익", "예상 연손익", "손익률"]}
                  rows={deptRows.map((x) => [x.name, man(x.예상연수익) + "원", man(x.예상연손익) + "원", `${x.손익률}%`])}
                  empty="" />
                <p className="mt-1 text-xs text-slate-400">최근 6개월 진료과 월평균(보험 결정수익·배부원가·삭감)에 매출 성장률·고정비 가정을 적용한 연환산.
                  배부원가는 고정성으로, 삭감은 활동(수익)에 비례 가정.</p>
              </>}
          </Card>

          <Card title="시나리오 저장·비교" action={
            <div className="flex gap-2">
              <button onClick={saveScenario} className="text-xs font-medium text-blue-600 hover:underline">현재 저장</button>
              <button onClick={exportPdf} className="text-xs font-medium text-blue-600 hover:underline">PDF 내보내기</button>
              <button onClick={() => exportCsv(rows)} className="text-xs font-medium text-blue-600 hover:underline">CSV 내보내기</button>
            </div>}>
            <Table headers={["비교", "시나리오", "성장률", "변동비율", "연 순이익", ""]}
              rows={[
                ["", "(현재)", `${(d.growth * 100).toFixed(1)}%`, `${(d.varRatio * 100).toFixed(0)}%`, man(annualNet) + "원", ""],
                ...scenarios.map((s) => [
                  <input key="c" type="checkbox" checked={cmp.includes(s.id)}
                    onChange={(e) => setCmp(e.target.checked ? [...cmp, s.id] : cmp.filter((x) => x !== s.id))} />,
                  s.name, `${(s.drivers.growth * 100).toFixed(1)}%`,
                  `${(s.drivers.varRatio * 100).toFixed(0)}%`, man(annualNetOf(b, s.drivers)) + "원",
                  <span key="a" className="flex gap-1">
                    <button onClick={() => setD(s.drivers)} className="text-xs text-blue-600">불러오기</button>
                    <button onClick={() => delMut.mutate(s.id)} className="text-xs text-red-500">삭제</button>
                  </span>])]}
              empty={scnQuery.isLoading ? "불러오는 중…" : "저장된 시나리오 없음"} />
            {cmpNames.length > 0 && (
              <div className="mt-3">
                <p className="mb-1 text-xs font-medium text-slate-600">월별 순이익 오버레이 — 현재 vs 선택 시나리오</p>
                <ResponsiveContainer width="100%" height={220}>
                  <ComposedChart data={overlay} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                    <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                    <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={64} />
                    <Tooltip formatter={(v) => won(Number(v))} /><Legend wrapperStyle={{ fontSize: 11 }} />
                    <Line dataKey="현재" stroke="#3b82f6" dot={false} strokeWidth={2} />
                    {cmpNames.map((n, i) => (
                      <Line key={n} dataKey={n} stroke={OVERLAY_COLORS[i % OVERLAY_COLORS.length]}
                        dot={false} strokeWidth={2} strokeDasharray="4 2" />
                    ))}
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function Kpi({ label, value, tone }: { label: string; value: string; tone?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <p className={`text-lg font-bold ${tone ?? "text-slate-800"}`}>{value}</p>
    </div>
  );
}
