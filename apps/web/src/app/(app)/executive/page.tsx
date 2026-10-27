"use client";
/** 경영진 종합 대시보드 — 위젯(막대/도넛/KPI) + 편집기능(표시·형태·순서, 브라우저 저장). */
import { scaleWon } from "@/components/kpi";
import { monthLocal } from "@/lib/dates";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { Bar, BarChart as RBarChart, CartesianGrid, Cell, Legend, ResponsiveContainer,
  Tooltip, XAxis, YAxis } from "recharts";
import { api } from "@/lib/api";
import { Baseline, forecastSummary } from "@/lib/sim";
import { Badge, Button, Card, Modal, Spinner, Table } from "@/components/ui";
import { BarChart, DonutChart } from "@/components/charts";
import { man, won } from "@/lib/format";

type Val = number | boolean | null;
type Exec = Record<string, Record<string, Val>> & { yyyymm: string };
type ChartType = "bar" | "donut" | "kpi";
type Unit = "won" | "pct" | "count";
interface Metric { key: string; label: string }
interface Widget { id: string; title: string; type: ChartType; unit: Unit; metrics: Metric[]; visible: boolean }

const LS_KEY = "exec-layout-v1";
const PALETTE = ["#3b82f6", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6", "#06b6d4"];

// 기획: 도메인 지표에 적절한 그래프 형태 기본값 (사용자가 편집기로 조정 가능)
const DEFAULTS: Widget[] = [
  { id: "pl", title: "손익 구성", type: "bar", unit: "won", visible: true, metrics: [
    { key: "재무.수익", label: "수익" }, { key: "재무.비용", label: "비용" },
    { key: "재무.당기순이익", label: "순이익" }] },
  { id: "rev", title: "수익(매출)", type: "kpi", unit: "won", visible: true, metrics: [
    { key: "재무.수익", label: "수익" }, { key: "재무.당기순이익", label: "당기순이익" }] },
  { id: "net", title: "당기순이익", type: "kpi", unit: "won", visible: true, metrics: [
    { key: "재무.당기순이익", label: "당기순이익" }, { key: "재무.수익", label: "수익" },
    { key: "재무.비용", label: "비용" }] },
  { id: "bs", title: "재무상태", type: "bar", unit: "won", visible: true, metrics: [
    { key: "재무.자산총계", label: "자산" }, { key: "재무.부채총계", label: "부채" },
    { key: "정합.자본총계", label: "자본" }] },
  { id: "asset", title: "고정자산", type: "kpi", unit: "won", visible: false, metrics: [
    { key: "자산.장부가총계", label: "장부가총계" }] },
  { id: "ratio", title: "재무비율", type: "bar", unit: "pct", visible: true, metrics: [
    { key: "재무비율.부채비율_%", label: "부채비율" }, { key: "재무비율.의료이익률_%", label: "의료이익률" },
    { key: "재무비율.인건비율_%", label: "인건비율" }] },
  { id: "voucher", title: "검진권", type: "bar", unit: "won", visible: true, metrics: [
    { key: "검진권.발급액", label: "발급" }, { key: "검진권.사용액", label: "사용" },
    { key: "검진권.미사용잔액", label: "미사용잔액" }] },
  { id: "cash", title: "자금 현황", type: "kpi", unit: "won", visible: true, metrics: [
    { key: "자금.기초현금", label: "기초현금" }, { key: "자금.6개월_최저잔액", label: "6개월 최저잔액" }] },
  { id: "care", title: "진료·인사", type: "kpi", unit: "count", visible: true, metrics: [
    { key: "진료.환자수", label: "환자수" }, { key: "진료.당월접수", label: "당월접수" },
    { key: "인사.재직인원", label: "재직인원" }] },
  { id: "fixed", title: "고정비용", type: "kpi", unit: "won", visible: true, metrics: [
    { key: "고정비용.고정비합계", label: "고정비합계" }, { key: "고정비용.인건비", label: "인건비" },
    { key: "고정비용.감가상각", label: "감가상각" }] },
  { id: "var", title: "변동비용", type: "kpi", unit: "won", visible: true, metrics: [
    { key: "변동비용.변동비합계", label: "변동비합계" }, { key: "변동비용.재료비", label: "재료비" }] },
  { id: "deduction", title: "보험 삭감", type: "kpi", unit: "won", visible: false, metrics: [
    { key: "보험.삭감손실", label: "삭감손실" }] },
  { id: "inventory", title: "재고", type: "kpi", unit: "count", visible: false, metrics: [
    { key: "재고.안전재고미달", label: "안전재고미달" }, { key: "재고.장기재고건수", label: "장기재고" }] },
  { id: "forecast", title: "재무 예측 (12개월)", type: "kpi", unit: "won", visible: true, metrics: [] },
];

const fmtUnit = (u: Unit, n: number) =>
  u === "won" ? man(n) : u === "pct" ? `${n}%` : n.toLocaleString();
// KPI 셀용 값/단위 분리 — 단위는 숫자의 ~55%로 축소(kpi.tsx 토큰·won=만원/억원 자동 스케일)
const fmtParts = (u: Unit, n: number): { value: string; unit: string } =>
  u === "won" ? scaleWon(n) : u === "pct" ? { value: String(n), unit: "%" }
  : { value: n.toLocaleString(), unit: "" };

// 저장된 레이아웃 + 신규 기본 위젯 병합(기본값 추가 시 유실 방지)
function loadLayout(): Widget[] {
  try {
    const saved: Widget[] = JSON.parse(localStorage.getItem(LS_KEY) || "[]");
    if (!Array.isArray(saved) || !saved.length) return DEFAULTS;
    const ids = new Set(saved.map((w) => w.id));
    return [...saved, ...DEFAULTS.filter((d) => !ids.has(d.id))];
  } catch { return DEFAULTS; }
}

// 드릴다운 가능한 위젯 → 모달 제목
const DRILLABLE: Record<string, string> = {
  pl: "손익 구성 분석 (드릴다운)", ratio: "재무비율 산식 (드릴다운)", voucher: "검진권 거래 상세 (드릴다운)",
  fixed: "고정비용 분석 (드릴다운)", net: "당기순이익 분석 (드릴다운)", bs: "재무상태 분석 (드릴다운)",
  rev: "수익(매출) 분석 (드릴다운)", var: "변동비용 분석 (드릴다운)",
  asset: "고정자산 분석 (드릴다운)", cash: "자금 현황 분석 (드릴다운)",
  inventory: "재고 분석 (드릴다운)", care: "진료·인사 분석 (드릴다운)",
};
// 고정비(영업) 계정 — 인건비·감가상각·리스
const FIXED_ACCTS: [string, string][] = [
  ["5310", "급여"], ["5320", "퇴직급여"], ["5210", "감가상각비"],
  ["5640", "사용권자산상각(리스)"], ["5630", "리스이자"]];

function DrillBar({ data }: { data: { label: string; value: number }[] }) {
  return (
    <ResponsiveContainer width="100%" height={240}>
      <RBarChart data={data} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
        <XAxis dataKey="label" tick={{ fontSize: 11 }} />
        <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={72} />
        <Tooltip formatter={(v) => man(Number(v))} />
        <Bar dataKey="value" radius={[3, 3, 0, 0]}>
          {data.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
        </Bar>
      </RBarChart>
    </ResponsiveContainer>
  );
}

interface AcctRow { account_code: string; name: string; amount: number }
interface DrillData {
  revenues?: AcctRow[]; expenses?: AcctRow[]; total_revenue?: number; // 손익계산서(income)
  total_expense?: number; net_income?: number;
  assets?: AcctRow[]; liabilities?: AcctRow[]; capital?: AcctRow[];   // 재무상태표(balance-sheet)
  total_assets?: number; total_liabilities?: number; equity?: number;
  retained_earnings?: number; capital_total?: number;
  labor_by_department?: { department: string | null; labor_cost: number; headcount: number }[];
  deduction_by_dept_code?: { dept_code: string | null; deduction_loss: number;
    claimed?: number; decided?: number }[];
  current_ratio?: number | null; debt_ratio?: number | null; equity_ratio?: number | null;
  operating_margin?: number | null; labor_cost_ratio?: number | null; material_cost_ratio?: number | null;
  basis?: Record<string, number>;
  by_line_type?: { line_type: string; count: number; amount: number }[];
  settlement_callbacks?: Record<string, number>; unredeemed_krw?: number;
  issued?: { amount: number; count: number }; redeemed?: { amount: number; count: number };
}
interface TrendData { months: { month: string; revenue: number; expense: number; net: number }[] }
interface AssetData { items: { asset_no: string; name: string; category: string | null;
  book_value: number; status: string }[] }
interface CashData { opening_cash: number; min_balance: number;
  projection: { label: string; inflow: number; outflow: number; net: number; balance: number }[] }
interface InvData { items?: { code: string; name: string; value?: number; current_stock?: number;
  turnover?: number; dead_stock?: boolean }[]; total_value?: number; count?: number; dead_stock_count?: number }
interface OverviewData { 인사: Record<string, number>; 진료: Record<string, number> }
interface LinesData { dept_code: string; count: number;
  items: { stmt_serial: string; claimed: number; decided: number; adjusted: number; deduction: number }[] }

// 위젯 한 줄 설명(대시보드 카드) — 모든 위젯
const WHAT: Record<string, string> = {
  pl: "수익·비용·당기순이익 구성", rev: "매출(수익) — 계정·진료과·추세", net: "수익−비용=당기순이익",
  bs: "자산·부채·자본 구성", ratio: "유동·부채·수익성 비율 (연간)", voucher: "검진권 발급·사용·잔액",
  cash: "현금 포지션·6개월 최저", care: "환자·접수·재직",
  fixed: "고정비(인건비·감가상각·리스)", var: "변동비(재료비 등)",
  asset: "사용중 고정자산 장부가", deduction: "보험 삭감손실", inventory: "안전재고·장기재고",
  forecast: "최근 추세 기준 12개월 예측 (시뮬레이터에서 가정 조정)",
};
// 드릴 상세 설명(모달) — 의미·계산식·데이터 출처·해석
const DESC: Record<string, { calc: string; source: string; read: string }> = {
  pl: { calc: "수익=수익계정 대변합, 비용=비용계정 차변합, 순이익=수익−비용",
    source: "전표(일마감·발생) → 손익계산서·월별추세·진료과 원가",
    read: "순이익 적자(빨강)면 비용·삭감 점검. 진료과 삭감은 '라인 ▸'로 명세까지 추적." },
  ratio: { calc: "유동=유동자산/유동부채, 부채=부채/자본, 의료이익률=순이익/수익, 인건비율=인건비/수익",
    source: "재무상태표·손익계산서(연간)",
    read: "유동비율 1배 미만=단기지급 주의, 인건비율 과다=수익성 압박." },
  voucher: { calc: "발급/사용=전표 차변합, 미사용잔액=선수금(2140) 잔액",
    source: "his.voucher 전표·딜러 정산콜백",
    read: "미사용잔액↑=선수수익(부채). 미완료콜백(SENT 외)은 정산 지연 점검." },
  fixed: { calc: "고정비=급여+퇴직급여+감가상각+사용권자산상각+리스이자(재료비 제외), 고정비율=고정비/수익",
    source: "손익계산서 비용 계정(전표)",
    read: "고정비율↑(수익 둔화 시 압박). 인건비·감가상각·리스 비중과 전년 동월 증감 추적." },
  net: { calc: "당기순이익=수익−총비용. 구조=수익−고정비−변동비−기타비용. 영업이익률=순이익/수익",
    source: "손익계산서(전표)·계정과목 원가구분(cost_type)",
    read: "순이익 적자면 고정비율·변동비 점검. 월별 추세·전년 동월로 추세 전환 확인." },
  bs: { calc: "자산=부채+자본, 자본=자산−부채(누적순이익 포함). 계정별 잔액=차변−대변 누적",
    source: "재무상태표(전표 기준일 누적)",
    read: "부채비율↑·자본 잠식 주의. 전년 동시점 대비 자산·부채·자본 증감 확인." },
  rev: { calc: "수익=수익계정 대변합. 진료과 수익=보험 결정액(claims decided) by 과코드",
    source: "손익계산서·청구 명세(전표)",
    read: "수익 계정·진료과 비중과 월별·전년 동월 추세로 매출 동향 확인." },
  var: { calc: "변동비=cost_type='VARIABLE' 계정(재료비 등) 합, 변동비율=변동비/수익",
    source: "손익계산서 비용 계정(전표)·계정과목 원가구분",
    read: "변동비율은 진료량 연동. 비정상 급증 시 재료·소모품 단가·사용량 점검." },
  asset: { calc: "장부가=취득가−감가상각누계. 사용중(IN_USE) 자산만 집계",
    source: "고정자산 대장(취득·감가·처분 전표)",
    read: "장부가 큰 자산·분류 비중 확인. 노후 자산 교체·감가 추세 점검." },
  cash: { calc: "기초현금 + 월별 AR 수금예정 − AP 지급예정 = 월말 잔액 추정(6개월)",
    source: "현금잔액·채권(AR)·채무(AP) 만기",
    read: "최저잔액 음수면 자금 부족 경고 — 수금 가속·지급 조정 필요." },
  inventory: { calc: "평가=FIFO 원가, 회전율=출고/현재고, 데드스톡=출고0 보유",
    source: "재고 Lot·이동(전표)",
    read: "안전재고 미달=발주, 데드스톡=처분·반품 검토." },
  care: { calc: "재직인원·환자수·접수·예약 + 월인건비·인건비율(인건비/수익)",
    source: "인사·환자·손익 집계",
    read: "인건비율 과다 시 수익성 압박. 진료량 대비 인력 적정성 점검." },
};

function DescBox({ id }: { id: string }) {
  const d = DESC[id]; if (!d) return null;
  return (
    <div className="rounded-lg border border-slate-200 bg-slate-50/60 p-3 text-xs text-slate-600">
      <p className="mb-1 font-semibold text-slate-700">📖 설명</p>
      <p><span className="text-slate-400">계산식</span> {d.calc}</p>
      <p><span className="text-slate-400">데이터 출처</span> {d.source}</p>
      <p><span className="text-slate-400">해석</span> {d.read}</p>
    </div>
  );
}

// 재무 예측 위젯 — sim-baseline을 받아 시뮬레이터와 동일 투영식(forecastSummary)으로 KPI 표시
function ForecastBody({ ym }: { ym: string }) {
  const q = useQuery({ queryKey: ["exec-forecast", ym],
    queryFn: () => api<Baseline>(`/analytics/sim-baseline?yyyymm=${ym}`) });
  if (q.isLoading) return <p className="text-xs text-slate-400">불러오는 중…</p>;
  if (q.isError || !q.data) return <p className="text-xs text-slate-400">예측 데이터 없음</p>;
  const f = forecastSummary(q.data);
  const cells = [
    { label: "예측 연순이익", value: man(f.annualNet), tone: f.annualNet < 0 ? "text-red-600" : "text-slate-800" },
    { label: "순이익률", value: `${f.margin.toFixed(1)}%`, tone: "text-slate-800" },
    { label: "손익분기(월매출)", value: man(f.bep), tone: "text-slate-800" },
    { label: "안전한계율", value: `${f.safety.toFixed(1)}%`, tone: f.safety < 0 ? "text-red-600" : "text-slate-800" },
    { label: "6개월 최저현금", value: man(f.minCash), tone: f.minCash < 0 ? "text-red-600" : "text-slate-800" },
    { label: "자금부족 시점", value: f.shortage ?? "없음", tone: f.shortage ? "text-red-600" : "text-emerald-600" },
  ];
  return (
    <dl className="grid grid-cols-2 gap-2">
      {cells.map((c) => (
        <div key={c.label} className="rounded-lg border border-slate-200 p-2">
          <dt className="text-xs text-slate-500">{c.label}</dt>
          <dd className={`text-base font-bold ${c.tone}`}>{c.value}</dd>
        </div>
      ))}
      <p className="col-span-2 text-[11px] text-slate-400">최근 추세(월 성장률 {(f.growth * 100).toFixed(1)}%) 가정. 시뮬레이터에서 가정 조정.</p>
    </dl>
  );
}

// 월 경계(YYYY-MM → from/to) — 검진권 드릴 등 기간 파라미터용
function monthRange(ym: string): [string, string] {
  const [y, m] = ym.split("-").map(Number);
  const last = new Date(y, m, 0).getDate();
  return [`${ym}-01`, `${ym}-${String(last).padStart(2, "0")}`];
}

function DrillModal({ widgetId, ym, onClose }: { widgetId: string; ym: string; onClose: () => void }) {
  const year = Number(ym.slice(0, 4));
  const [range0, range1] = monthRange(ym);
  const [subDept, setSubDept] = useState<string | null>(null);   // 2단계: 진료과 → 라인
  const incomeWidget = ["pl", "fixed", "net", "rev", "var"].includes(widgetId);
  const [pRange0, pRange1] = monthRange(`${year - 1}-${ym.slice(5, 7)}`);
  const ep = incomeWidget ? `/finance/statements/income?start=${range0}&end=${range1}`
    : widgetId === "ratio" ? `/finance/statements/financial-ratios?year=${year}`
      : widgetId === "bs" ? `/finance/statements/balance-sheet?as_of=${range1}`
        : `/analytics/voucher?date_from=${range0}&date_to=${range1}`;
  const selfFetch = ["asset", "cash", "inventory", "care"].includes(widgetId);
  const q = useQuery({ queryKey: ["drill", widgetId, ym], enabled: !selfFetch,
    queryFn: () => api<DrillData>(ep) });
  const assetQ = useQuery({ queryKey: ["drill-asset"], enabled: widgetId === "asset",
    queryFn: () => api<AssetData>("/finance/assets") });
  const cashQ = useQuery({ queryKey: ["drill-cash", ym], enabled: widgetId === "cash",
    queryFn: () => api<CashData>(`/finance/cash-forecast?as_of=${range1}&months=6`) });
  const valQ = useQuery({ queryKey: ["drill-val"], enabled: widgetId === "inventory",
    queryFn: () => api<InvData>("/inventory/valuation") });
  const shortageQ = useQuery({ queryKey: ["drill-shortage"], enabled: widgetId === "inventory",
    queryFn: () => api<InvData>("/inventory/shortage") });
  const turnoverQ = useQuery({ queryKey: ["drill-turnover", ym], enabled: widgetId === "inventory",
    queryFn: () => api<InvData>(`/inventory/turnover?since=${range0}&until=${range1}`) });
  const overviewQ = useQuery({ queryKey: ["drill-overview", ym], enabled: widgetId === "care",
    queryFn: () => api<OverviewData>(`/analytics/overview?yyyymm=${ym}`) });
  const turnoverQ2 = useQuery({ queryKey: ["drill-turnover2", ym], enabled: widgetId === "care",
    queryFn: () => api<{ monthly: { month: string; joined: number; left: number }[];
      by_dept: { department: string; active: number; left: number; rate: number }[];
      by_reason: { reason: string; count: number }[] }>(
      `/analytics/hr-turnover?yyyymm=${ym}`) });
  const bsPrevQ = useQuery({ queryKey: ["drill-bs-prev", ym], enabled: widgetId === "bs",
    queryFn: () => api<DrillData>(`/finance/statements/balance-sheet?as_of=${pRange1}`) });
  const incPrevQ = useQuery({ queryKey: ["drill-inc-prev", ym], enabled: widgetId === "fixed" || widgetId === "var",
    queryFn: () => api<DrillData>(`/finance/statements/income?start=${pRange0}&end=${pRange1}`) });
  const fixedAcctsQ = useQuery({ queryKey: ["drill-fixed-accts"], enabled: widgetId === "fixed",
    queryFn: () => api<{ items: { code: string; name: string }[] }>("/finance/accounts?cost_type=FIXED") });
  const varAcctsQ = useQuery({ queryKey: ["drill-var-accts"], enabled: widgetId === "var",
    queryFn: () => api<{ items: { code: string; name: string }[] }>("/finance/accounts?cost_type=VARIABLE") });
  const voucherPrevQ = useQuery({ queryKey: ["drill-voucher-prev", ym], enabled: widgetId === "voucher",
    queryFn: () => api<DrillData>(`/analytics/voucher?date_from=${pRange0}&date_to=${pRange1}`) });
  const expAcctsQ = useQuery({ queryKey: ["drill-exp-accts"], enabled: widgetId === "net",
    queryFn: () => api<{ items: { code: string; cost_type: string | null }[] }>("/finance/accounts?kind=EXPENSE") });
  const usesTrend = widgetId === "pl" || widgetId === "net" || widgetId === "rev";
  const trendQ = useQuery({ queryKey: ["drill-trend", year], enabled: usesTrend,
    queryFn: () => api<TrendData>(`/analytics/trend?year=${year}`) });
  const trendPrevQ = useQuery({ queryKey: ["drill-trend", year - 1], enabled: usesTrend,
    queryFn: () => api<TrendData>(`/analytics/trend?year=${year - 1}`) });
  const deptQ = useQuery({ queryKey: ["drill-dept", ym], enabled: widgetId === "pl" || widgetId === "rev",
    queryFn: () => api<DrillData>(`/analytics/department-pl?yyyymm=${ym}`) });
  const ratioPrevQ = useQuery({ queryKey: ["drill-ratio", year - 1], enabled: widgetId === "ratio",
    queryFn: () => api<DrillData>(`/finance/statements/financial-ratios?year=${year - 1}`) });
  const lines = useQuery({ queryKey: ["drill-lines", ym, subDept], enabled: !!subDept,
    queryFn: () => api<LinesData>(`/analytics/department-pl/lines?yyyymm=${ym}&dept_code=${encodeURIComponent(subDept ?? "")}`) });
  const d = q.data;
  const pct = (v: number | null | undefined) => v == null ? "—" : `${(v * 100).toFixed(1)}%`;

  // 2단계 라인 패널 (진료과 클릭 시)
  if (subDept !== null) {
    return (
      <Modal title={`${subDept} — 명세 라인 (${ym})`} size="lg" onClose={onClose}>
        <button onClick={() => setSubDept(null)} className="mb-3 text-xs font-medium text-blue-600 hover:underline">← 진료과로</button>
        {lines.isLoading ? <Spinner /> : lines.isError ? <p className="text-sm text-red-600">권한 또는 데이터 오류</p> : (
          <>
            <DrillBar data={(lines.data?.items ?? []).slice(0, 15).map((r) => ({ label: r.stmt_serial, value: r.deduction }))} />
            <Table headers={["명세일련", "청구", "결정", "조정", "삭감"]}
              rows={(lines.data?.items ?? []).map((r) => [r.stmt_serial,
                won(r.claimed), won(r.decided),
                won(r.adjusted), won(r.deduction)])}
              empty="명세 없음" />
          </>
        )}
      </Modal>
    );
  }

  return (
    <Modal title={DRILLABLE[widgetId]} size="lg" onClose={onClose}>
      {q.isLoading ? <Spinner /> : q.isError ? <p className="text-sm text-red-600">권한 또는 데이터 오류</p> : !d ? null : (
        <div className="space-y-4">
          {widgetId === "pl" && (() => {
            const mm = ym.slice(5, 7);
            const cur = trendQ.data?.months ?? [];
            const prv = trendPrevQ.data?.months ?? [];
            const find = (arr: TrendData["months"], y: number) => arr.find((m) => m.month === `${y}-${mm}`);
            const c = find(cur, year), p = find(prv, year - 1);
            const yoy = (cv: number, pv: number) =>
              pv ? `${cv - pv >= 0 ? "+" : ""}${(((cv - pv) / Math.abs(pv)) * 100).toFixed(1)}%` : "—";
            const cmp = Array.from({ length: 12 }, (_, k) => {
              const m = String(k + 1).padStart(2, "0");
              return { label: m,
                올해: cur.find((x) => x.month === `${year}-${m}`)?.net ?? 0,
                전년: prv.find((x) => x.month === `${year - 1}-${m}`)?.net ?? 0 };
            });
            return (<>
              <p className="text-xs font-medium text-slate-500">당월 vs 전년 동월 ({ym} ↔ {year - 1}-{mm})</p>
              <Table headers={["항목", "당월", "전년 동월", "증감률"]} rows={[
                ["수익", won(c?.revenue ?? 0), won(p?.revenue ?? 0), yoy(c?.revenue ?? 0, p?.revenue ?? 0)],
                ["비용", won(c?.expense ?? 0), won(p?.expense ?? 0), yoy(c?.expense ?? 0, p?.expense ?? 0)],
                ["순이익", won(c?.net ?? 0), won(p?.net ?? 0), yoy(c?.net ?? 0, p?.net ?? 0)]]} />
              <p className="text-xs font-medium text-slate-500">월별 순이익 — 올해 vs 전년</p>
              <ResponsiveContainer width="100%" height={240}>
                <RBarChart data={cmp} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={72} />
                  <Tooltip formatter={(v) => man(Number(v))} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="전년" fill="#cbd5e1" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="올해" fill="#3b82f6" radius={[3, 3, 0, 0]} />
                </RBarChart>
              </ResponsiveContainer>
              <div className="grid gap-4 md:grid-cols-2">
                <div>
                  <p className="mb-1 text-xs font-medium text-slate-500">수익 계정별</p>
                  <Table headers={["계정", "금액"]}
                    rows={(d.revenues ?? []).map((r) => [`${r.account_code} ${r.name}`, won(r.amount)])}
                    empty="수익 없음" />
                </div>
                <div>
                  <p className="mb-1 text-xs font-medium text-slate-500">비용 계정별</p>
                  <Table headers={["계정", "금액"]}
                    rows={(d.expenses ?? []).map((r) => [`${r.account_code} ${r.name}`, won(r.amount)])}
                    empty="비용 없음" />
                </div>
              </div>
              <p className="text-xs font-medium text-slate-500">진료과별 인건비·삭감 (과코드 라인 ▸ → 명세 추적)</p>
              <Table headers={["진료과", "인건비", "인원"]}
                rows={(deptQ.data?.labor_by_department ?? []).map((r) => [r.department ?? "-", won(r.labor_cost), `${r.headcount}명`])}
                empty="인건비 데이터 없음" />
              <Table headers={["과코드", "삭감손실", ""]}
                rows={(deptQ.data?.deduction_by_dept_code ?? []).map((r) => [r.dept_code ?? "-",
                  won(r.deduction_loss ?? 0),
                  r.dept_code
                    ? <button key="d" onClick={() => setSubDept(r.dept_code)}
                        className="text-xs font-medium text-blue-600 hover:underline">라인 ▸</button>
                    : ""])}
                empty="삭감 없음" />
              <DescBox id="pl" />
            </>);
          })()}

          {widgetId === "ratio" && (() => {
            const pv = ratioPrevQ.data;
            const mult = (v: number | null | undefined) => v == null ? "—" : `${v.toFixed(2)}배`;
            const dpp = (c: number | null | undefined, p: number | null | undefined) =>
              c == null || p == null ? "—" : `${(c - p) * 100 >= 0 ? "+" : ""}${((c - p) * 100).toFixed(1)}p`;
            const rows: [string, (v: number | null | undefined) => string, keyof DrillData][] = [
              ["유동비율", mult, "current_ratio"], ["부채비율", pct, "debt_ratio"],
              ["자기자본비율", pct, "equity_ratio"], ["의료이익률", pct, "operating_margin"],
              ["인건비율", pct, "labor_cost_ratio"], ["재료비율", pct, "material_cost_ratio"]];
            return (<>
              <p className="text-xs font-medium text-slate-500">재무비율 — {year} vs {year - 1}</p>
              <Table headers={["지표", `${year}`, `${year - 1}`, "증감"]} rows={rows.map(([label, f, key]) => {
                const c = d[key] as number | null | undefined;
                const p = pv?.[key] as number | null | undefined;
                return [label, f(c), f(p),
                  key === "current_ratio"
                    ? (c == null || p == null ? "—" : `${c - p >= 0 ? "+" : ""}${(c - p).toFixed(2)}배`)
                    : dpp(c, p)];
              })} />
              <p className="text-xs font-medium text-slate-500">산식 근거 ({year})</p>
              <Table headers={["항목", "금액"]} rows={(
                [["유동자산", "current_assets"], ["유동부채", "current_liabilities"],
                  ["자산총계", "total_assets"], ["부채총계", "total_liabilities"], ["자본총계", "equity"],
                  ["수익", "revenue"], ["순이익", "net_income"], ["인건비", "labor"], ["재료비", "material"]]
              ).map(([k, key]) => [k, won(Number(d.basis?.[key] ?? 0))])} />
              <DescBox id="ratio" />
            </>);
          })()}

          {widgetId === "voucher" && (
            <>
              {(() => {
                const pv = voucherPrevQ.data;
                const yoy = (c: number, p: number | undefined) =>
                  p ? `${c - p >= 0 ? "+" : ""}${(((c - p) / Math.abs(p)) * 100).toFixed(1)}%` : "—";
                const ci = d.issued?.amount ?? 0, cr = d.redeemed?.amount ?? 0;
                return (
                  <Table headers={["항목", "당월", `전년(${pRange1.slice(0, 7)})`, "증감"]} rows={[
                    ["발급액", won(ci), won(pv?.issued?.amount ?? 0), yoy(ci, pv?.issued?.amount)],
                    ["사용액", won(cr), won(pv?.redeemed?.amount ?? 0), yoy(cr, pv?.redeemed?.amount)]]} />
                );
              })()}
              <DrillBar data={(d.by_line_type ?? []).map((r) => ({ label: r.line_type, value: r.amount }))} />
              <Table headers={["거래유형", "건수", "금액"]}
                rows={(d.by_line_type ?? []).map((r) => [r.line_type, `${r.count}건`, won(r.amount)])} />
              <div className="flex flex-wrap gap-2 text-sm">
                <Badge tone="blue">미사용잔액 {won(d.unredeemed_krw ?? 0)}</Badge>
                {Object.entries(d.settlement_callbacks ?? {}).map(([s, c]) => (
                  <Badge key={s} tone={s === "SENT" ? "green" : "amber"}>{s} {String(c)}</Badge>))}
              </div>
              <DescBox id="voucher" />
            </>
          )}

          {widgetId === "fixed" && (() => {
            const rev = d.total_revenue ?? 0;
            const amt = (exp: AcctRow[] | undefined, code: string) =>
              (exp ?? []).filter((e) => e.account_code === code).reduce((a, e) => a + e.amount, 0);
            // 설정값(cost_type=FIXED) 기반, 미로딩 시 기본 계정 폴백
            const accts: [string, string][] = (fixedAcctsQ.data?.items ?? []).length
              ? fixedAcctsQ.data!.items.map((x) => [x.code, x.name] as [string, string]) : FIXED_ACCTS;
            const rows = accts.map(([code, label]) => {
              const c = amt(d.expenses, code), p = amt(incPrevQ.data?.expenses, code);
              return { code, label, c, p,
                pctRev: rev ? `${(c / rev * 100).toFixed(1)}%` : "—",
                yoy: p ? `${c - p >= 0 ? "+" : ""}${(((c - p) / Math.abs(p)) * 100).toFixed(1)}%` : "—" };
            });
            const total = rows.reduce((a, r) => a + r.c, 0);
            return (<>
              <DrillBar data={rows.map((r) => ({ label: r.label, value: r.c }))} />
              <Table headers={["계정", "당월", "수익대비", "전년동월", "증감"]}
                rows={rows.map((r) => [r.label, won(r.c), r.pctRev,
                  won(r.p), r.yoy])} />
              <div className="flex flex-wrap gap-2 text-sm">
                <Badge tone="blue">고정비 합계 {won(total)}</Badge>
                <Badge tone={rev && total / rev > 0.6 ? "red" : "gray"}>
                  고정비율 {rev ? `${(total / rev * 100).toFixed(1)}%` : "—"}</Badge>
              </div>
              <Link href="/finance-accounts" onClick={onClose}
                className="inline-block text-xs font-medium text-blue-600 hover:underline">
                고정/변동 분류 설정 (계정과목 관리) ▸</Link>
              <DescBox id="fixed" />
            </>);
          })()}

          {widgetId === "net" && (() => {
            const rev = d.total_revenue ?? 0;
            const totalExp = d.total_expense ?? 0;
            const net = d.net_income ?? (rev - totalExp);
            const ct = new Map((expAcctsQ.data?.items ?? []).map((x) => [x.code, x.cost_type]));
            const sumBy = (t: string) => (d.expenses ?? [])
              .filter((e) => ct.get(e.account_code) === t).reduce((a, e) => a + e.amount, 0);
            const fixed = sumBy("FIXED"), variable = sumBy("VARIABLE");
            const other = totalExp - fixed - variable;
            const pr = (v: number) => rev ? `${(v / rev * 100).toFixed(1)}%` : "—";
            const cur = trendQ.data?.months ?? [], prv = trendPrevQ.data?.months ?? [];
            const cmp = Array.from({ length: 12 }, (_, k) => {
              const m = String(k + 1).padStart(2, "0");
              return { label: m, 올해: cur.find((x) => x.month === `${year}-${m}`)?.net ?? 0,
                전년: prv.find((x) => x.month === `${year - 1}-${m}`)?.net ?? 0 };
            });
            return (<>
              <p className="text-xs font-medium text-slate-500">당월 손익 구조 ({ym})</p>
              <Table headers={["항목", "금액", "수익대비"]} rows={[
                ["수익", won(rev), "100%"],
                ["(−) 고정비", won(fixed), pr(fixed)],
                ["(−) 변동비", won(variable), pr(variable)],
                ["(−) 기타(비분류·영업외)", won(other), pr(other)],
                ["= 당기순이익", won(net), pr(net)]]} />
              <p className="text-xs font-medium text-slate-500">월별 순이익 — 올해 vs 전년</p>
              <ResponsiveContainer width="100%" height={240}>
                <RBarChart data={cmp} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={72} />
                  <Tooltip formatter={(v) => man(Number(v))} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="전년" fill="#cbd5e1" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="올해" fill="#3b82f6" radius={[3, 3, 0, 0]} />
                </RBarChart>
              </ResponsiveContainer>
              <DescBox id="net" />
            </>);
          })()}

          {widgetId === "bs" && (() => {
            const p = bsPrevQ.data;
            const yoy = (c: number, pv: number | undefined) =>
              pv ? `${c - pv >= 0 ? "+" : ""}${(((c - pv) / Math.abs(pv)) * 100).toFixed(1)}%` : "—";
            const ta = d.total_assets ?? 0, tl = d.total_liabilities ?? 0, eq = d.equity ?? 0;
            const acctRows = (list: AcctRow[] | undefined) =>
              (list ?? []).map((a) => [`${a.account_code} ${a.name}`, won(a.amount)]);
            return (<>
              <p className="text-xs font-medium text-slate-500">요약 ({range1} vs 전년 {pRange1})</p>
              <Table headers={["항목", "당기", "전년 동시점", "증감"]} rows={[
                ["자산총계", won(ta), won(p?.total_assets ?? 0), yoy(ta, p?.total_assets)],
                ["부채총계", won(tl), won(p?.total_liabilities ?? 0), yoy(tl, p?.total_liabilities)],
                ["자본총계", won(eq), won(p?.equity ?? 0), yoy(eq, p?.equity)]]} />
              <DrillBar data={[{ label: "자산", value: ta }, { label: "부채", value: tl }, { label: "자본", value: eq }]} />
              <div className="grid gap-4 md:grid-cols-2">
                <div><p className="mb-1 text-xs font-medium text-slate-500">자산 계정별</p>
                  <Table headers={["계정", "금액"]} rows={acctRows(d.assets)} empty="자산 없음" /></div>
                <div><p className="mb-1 text-xs font-medium text-slate-500">부채 계정별</p>
                  <Table headers={["계정", "금액"]} rows={acctRows(d.liabilities)} empty="부채 없음" /></div>
              </div>
              <p className="text-xs font-medium text-slate-500">자본 구성</p>
              <Table headers={["항목", "금액"]} rows={[
                ...(d.capital ?? []).map((a) => [`${a.account_code} ${a.name}`, won(a.amount)]),
                ["이익잉여금(누적순이익)", won(d.retained_earnings ?? 0)],
                ["자본총계", won(eq)]]} />
              <DescBox id="bs" />
            </>);
          })()}

          {widgetId === "rev" && (() => {
            const rev = d.total_revenue ?? 0;
            const pr = (v: number) => rev ? `${(v / rev * 100).toFixed(1)}%` : "—";
            const dept = (deptQ.data?.deduction_by_dept_code ?? [])
              .map((r) => ({ code: r.dept_code ?? "-", decided: r.decided ?? 0 }))
              .sort((a, b) => b.decided - a.decided);
            const cur = trendQ.data?.months ?? [], prv = trendPrevQ.data?.months ?? [];
            const cmp = Array.from({ length: 12 }, (_, k) => {
              const m = String(k + 1).padStart(2, "0");
              return { label: m, 올해: cur.find((x) => x.month === `${year}-${m}`)?.revenue ?? 0,
                전년: prv.find((x) => x.month === `${year - 1}-${m}`)?.revenue ?? 0 };
            });
            return (<>
              <DrillBar data={(d.revenues ?? []).map((r) => ({ label: r.name, value: r.amount }))} />
              <p className="text-xs font-medium text-slate-500">수익 계정별</p>
              <Table headers={["계정", "금액", "비중"]}
                rows={(d.revenues ?? []).map((r) => [`${r.account_code} ${r.name}`, won(r.amount), pr(r.amount)])}
                empty="수익 없음" />
              <p className="text-xs font-medium text-slate-500">진료과별 보험 결정수익</p>
              <Table headers={["과코드", "결정수익"]}
                rows={dept.map((r) => [r.code, won(r.decided)])} empty="청구 수익 없음" />
              <p className="text-xs font-medium text-slate-500">월별 수익 — 올해 vs 전년</p>
              <ResponsiveContainer width="100%" height={240}>
                <RBarChart data={cmp} margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                  <XAxis dataKey="label" tick={{ fontSize: 11 }} />
                  <YAxis tickFormatter={(v) => man(Number(v))} tick={{ fontSize: 11 }} width={72} />
                  <Tooltip formatter={(v) => man(Number(v))} />
                  <Legend wrapperStyle={{ fontSize: 11 }} />
                  <Bar dataKey="전년" fill="#cbd5e1" radius={[3, 3, 0, 0]} />
                  <Bar dataKey="올해" fill="#3b82f6" radius={[3, 3, 0, 0]} />
                </RBarChart>
              </ResponsiveContainer>
              <DescBox id="rev" />
            </>);
          })()}

          {widgetId === "var" && (() => {
            const rev = d.total_revenue ?? 0;
            const amt = (exp: AcctRow[] | undefined, code: string) =>
              (exp ?? []).filter((e) => e.account_code === code).reduce((a, e) => a + e.amount, 0);
            const accts: [string, string][] = (varAcctsQ.data?.items ?? []).length
              ? varAcctsQ.data!.items.map((x) => [x.code, x.name] as [string, string])
              : [["5410", "재료비"]];
            const rows = accts.map(([code, label]) => {
              const c = amt(d.expenses, code), p = amt(incPrevQ.data?.expenses, code);
              return { code, label, c, p, pctRev: rev ? `${(c / rev * 100).toFixed(1)}%` : "—",
                yoy: p ? `${c - p >= 0 ? "+" : ""}${(((c - p) / Math.abs(p)) * 100).toFixed(1)}%` : "—" };
            });
            const total = rows.reduce((a, r) => a + r.c, 0);
            return (<>
              <DrillBar data={rows.map((r) => ({ label: r.label, value: r.c }))} />
              <Table headers={["계정", "당월", "수익대비", "전년동월", "증감"]}
                rows={rows.map((r) => [r.label, won(r.c), r.pctRev,
                  won(r.p), r.yoy])} />
              <div className="flex flex-wrap gap-2 text-sm">
                <Badge tone="blue">변동비 합계 {won(total)}</Badge>
                <Badge tone="gray">변동비율 {rev ? `${(total / rev * 100).toFixed(1)}%` : "—"}</Badge>
              </div>
              <Link href="/finance-accounts" onClick={onClose}
                className="inline-block text-xs font-medium text-blue-600 hover:underline">
                고정/변동 분류 설정 (계정과목 관리) ▸</Link>
              <DescBox id="var" />
            </>);
          })()}
        </div>
      )}

      {widgetId === "asset" && (assetQ.isLoading ? <Spinner /> : (() => {
        const inUse = (assetQ.data?.items ?? []).filter((a) => a.status === "IN_USE");
        const byCat = Object.entries(inUse.reduce((m, a) => {
          const k = a.category ?? "기타"; m[k] = (m[k] ?? 0) + a.book_value; return m;
        }, {} as Record<string, number>)).map(([label, value]) => ({ label, value }));
        return (
          <div className="space-y-4">
            <DrillBar data={byCat} />
            <Table headers={["자산번호", "명칭", "분류", "장부가"]}
              rows={inUse.sort((a, b) => b.book_value - a.book_value).map((a) => [
                a.asset_no, a.name, a.category ?? "-", won(Math.round(a.book_value))])}
              empty="사용중 자산 없음" />
            <DescBox id="asset" />
          </div>);
      })())}

      {widgetId === "cash" && (cashQ.isLoading ? <Spinner /> : (() => {
        const p = cashQ.data;
        return (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge tone="blue">기초현금 {won(p?.opening_cash ?? 0)}</Badge>
              <Badge tone={(p?.min_balance ?? 0) < 0 ? "red" : "gray"}>
                6개월 최저잔액 {won(p?.min_balance ?? 0)}</Badge>
            </div>
            <DrillBar data={(p?.projection ?? []).map((m) => ({ label: m.label, value: m.balance }))} />
            <Table headers={["월", "수금예정", "지급예정", "순", "예상잔액"]}
              rows={(p?.projection ?? []).map((m) => [m.label,
                won(m.inflow), won(m.outflow),
                won(m.net), won(m.balance)])}
              empty="예측 데이터 없음" />
            <DescBox id="cash" />
          </div>);
      })())}

      {widgetId === "inventory" && (valQ.isLoading ? <Spinner /> : (() => {
        const dead = (turnoverQ.data?.items ?? []).filter((i) => i.dead_stock);
        return (
          <div className="space-y-4">
            <div className="flex flex-wrap gap-2 text-sm">
              <Badge tone="blue">재고자산평가 {won(Math.round(valQ.data?.total_value ?? 0))}</Badge>
              <Badge tone={(shortageQ.data?.count ?? 0) > 0 ? "amber" : "gray"}>안전재고 미달 {shortageQ.data?.count ?? 0}건</Badge>
              <Badge tone={(turnoverQ.data?.dead_stock_count ?? 0) > 0 ? "amber" : "gray"}>장기재고 {turnoverQ.data?.dead_stock_count ?? 0}건</Badge>
            </div>
            <p className="text-xs font-medium text-slate-500">안전재고 미달</p>
            <Table headers={["코드", "품명"]}
              rows={(shortageQ.data?.items ?? []).slice(0, 10).map((i) => [i.code, i.name])} empty="미달 없음 ✓" />
            <p className="text-xs font-medium text-slate-500">장기재고(데드스톡)</p>
            <Table headers={["코드", "품명", "현재고"]}
              rows={dead.slice(0, 10).map((i) => [i.code, i.name, i.current_stock ?? 0])} empty="없음 ✓" />
            <DescBox id="inventory" />
          </div>);
      })())}

      {widgetId === "care" && (overviewQ.isLoading ? <Spinner /> : (() => {
        const h = overviewQ.data?.인사 ?? {}, p = overviewQ.data?.진료 ?? {};
        return (
          <div className="space-y-4">
            <p className="text-xs font-medium text-slate-500">인사</p>
            <Table headers={["항목", "값"]} rows={[
              ["재직인원", `${h["재직인원"] ?? 0}명`],
              ["월인건비", won(h["월인건비"] ?? 0)],
              ["인건비/수익", `${h["인건비_대_수익_%"] ?? 0}%`],
              ["최근1년 입사", `${h["최근1년입사"] ?? 0}명`],
              ["최근1년 퇴직", `${h["최근1년퇴직"] ?? 0}명`],
              ["이직률(최근1년)", `${h["이직률_%"] ?? 0}%`]]} />
            <p className="text-xs font-medium text-slate-500">진료</p>
            <Table headers={["항목", "값"]} rows={[
              ["환자수", `${(p["환자수"] ?? 0).toLocaleString()}명`],
              ["당월접수", `${p["당월접수"] ?? 0}건`], ["예약", `${p["예약"] ?? 0}건`]]} />
            <p className="text-xs font-medium text-slate-500">월별 입사 vs 퇴직 (최근 1년)</p>
            <ResponsiveContainer width="100%" height={220}>
              <RBarChart data={(turnoverQ2.data?.monthly ?? []).map((m) => ({
                label: m.month.slice(2), 입사: m.joined, 퇴직: m.left }))}
                margin={{ top: 8, right: 8, left: 8, bottom: 8 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#eef2f7" />
                <XAxis dataKey="label" tick={{ fontSize: 10 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} width={28} />
                <Tooltip /><Legend wrapperStyle={{ fontSize: 11 }} />
                <Bar dataKey="입사" fill="#10b981" radius={[3, 3, 0, 0]} />
                <Bar dataKey="퇴직" fill="#ef4444" radius={[3, 3, 0, 0]} />
              </RBarChart>
            </ResponsiveContainer>
            <p className="text-xs font-medium text-slate-500">진료과·부서별 이직률 (최근 1년)</p>
            <Table headers={["부서", "재직", "퇴직", "이직률"]}
              rows={(turnoverQ2.data?.by_dept ?? []).map((r) => [r.department, `${r.active}명`, `${r.left}명`, `${r.rate}%`])}
              empty="퇴직 없음" />
            <p className="text-xs font-medium text-slate-500">퇴직 사유별 (최근 1년)</p>
            <Table headers={["사유", "인원"]}
              rows={(turnoverQ2.data?.by_reason ?? []).map((r) => [r.reason, `${r.count}명`])}
              empty="퇴직 없음" />
            <DescBox id="care" />
          </div>);
      })())}
    </Modal>
  );
}

export default function ExecutivePage() {
  const [ym, setYm] = useState(monthLocal());
  // 저장된 레이아웃 lazy 로드(SSR=기본값, 클라=localStorage) → 마운트 후 setState 회피
  const [layout, setLayout] = useState<Widget[]>(() =>
    typeof window === "undefined" ? DEFAULTS : loadLayout());
  const [edit, setEdit] = useState(false);
  const [drill, setDrill] = useState<string | null>(null);

  useEffect(() => { localStorage.setItem(LS_KEY, JSON.stringify(layout)); }, [layout]);

  const ex = useQuery({ queryKey: ["exec", ym], queryFn: () => api<Exec>(`/analytics/executive?yyyymm=${ym}`) });

  const flat: Record<string, Val> = {};
  if (ex.data) for (const [g, obj] of Object.entries(ex.data)) {
    if (g === "yyyymm" || typeof obj !== "object" || obj === null) continue;
    for (const [k, v] of Object.entries(obj)) flat[`${g}.${k}`] = v;
  }
  const num = (key: string) => { const v = flat[key]; return typeof v === "number" ? v : 0; };

  const setType = (id: string, type: ChartType) =>
    setLayout((l) => l.map((w) => w.id === id ? { ...w, type } : w));
  const toggle = (id: string) =>
    setLayout((l) => l.map((w) => w.id === id ? { ...w, visible: !w.visible } : w));
  const move = (i: number, d: number) => setLayout((l) => {
    const a = [...l]; const j = i + d; if (j < 0 || j >= a.length) return l;
    [a[i], a[j]] = [a[j], a[i]]; return a;
  });

  const shown = layout.filter((w) => edit || w.visible);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">경영진 종합 대시보드</h1>
        <div className="flex items-center gap-2">
          {/* 경영 BI(Grafana) — reporting 마트 시계열·드릴 대시보드. nginx /erp-bi/ 라우트 활성 후 동작(런북). */}
          <a href="/erp/bi/" target="_blank" rel="noopener noreferrer"
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            경영 BI ↗</a>
          <input type="month" value={ym} onChange={(e) => setYm(e.target.value)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          {edit && <Button variant="secondary" onClick={() => setLayout(DEFAULTS)}>기본값 복원</Button>}
          <Button variant={edit ? "primary" : "secondary"} onClick={() => setEdit((v) => !v)}>
            {edit ? "편집 완료" : "편집"}</Button>
        </div>
      </div>

      {ex.isError && <p className="text-sm text-red-600">권한 또는 데이터 오류</p>}
      {edit && <p className="text-xs text-slate-400">위젯별 표시/형태/순서를 조정하세요 — 변경은 이 브라우저에 저장됩니다.</p>}

      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
        {shown.map((w) => {
          const i = layout.indexOf(w);
          const data = w.metrics.map((m, k) => ({ label: m.label, value: num(m.key),
            color: PALETTE[k % PALETTE.length] }));
          return (
            <Card key={w.id} title={w.title}
              action={!edit && w.id === "forecast"
                ? <Link href="/finance-simulation" className="text-xs font-medium text-blue-600 hover:underline">시뮬레이터 ▸</Link>
                : !edit && DRILLABLE[w.id]
                ? <button onClick={() => setDrill(w.id)}
                    className="text-xs font-medium text-blue-600 hover:underline">분석 ▸</button>
                : undefined}>
              {edit && (
                <div className="mb-2 flex flex-wrap items-center gap-1 border-b border-slate-100 pb-2 text-xs">
                  <label className="flex items-center gap-1">
                    <input type="checkbox" checked={w.visible} onChange={() => toggle(w.id)} />표시</label>
                  <select value={w.type} onChange={(e) => setType(w.id, e.target.value as ChartType)}
                    className="rounded border border-slate-300 px-1 py-0.5">
                    <option value="bar">막대</option><option value="donut">도넛</option>
                    <option value="kpi">KPI</option>
                  </select>
                  <button onClick={() => move(i, -1)} className="rounded border border-slate-200 px-1.5">↑</button>
                  <button onClick={() => move(i, 1)} className="rounded border border-slate-200 px-1.5">↓</button>
                  {!w.visible && <span className="text-slate-400">(숨김)</span>}
                </div>
              )}
              {WHAT[w.id] && <p className="mb-2 text-[11px] text-slate-400">{WHAT[w.id]}</p>}
              {w.id === "forecast" && <ForecastBody ym={ym} />}
              {w.id !== "forecast" && w.type === "bar" && <BarChart data={data} format={(n) => fmtUnit(w.unit, n)} />}
              {w.id !== "forecast" && w.type === "donut" && <DonutChart data={data} />}
              {w.id !== "forecast" && w.type === "kpi" && (
                <dl className="grid grid-cols-2 gap-2">
                  {w.metrics.map((m) => {
                    const parts = fmtParts(w.unit, num(m.key));
                    return (
                      <div key={m.key}
                        className="rounded-xl border border-slate-200 p-2.5 transition hover:shadow-sm">
                        <dt className="text-[11px] font-medium tracking-wide text-slate-400">{m.label}</dt>
                        <dd className={`mt-0.5 text-right tabular-nums leading-none ${
                          num(m.key) < 0 ? "text-red-600" : "text-slate-800"}`}>
                          <span className="text-lg font-bold">{parts.value}</span>
                          {parts.unit && <span className="ml-0.5 align-baseline text-[11px] font-medium text-slate-400">{parts.unit}</span>}
                        </dd>
                      </div>
                    );
                  })}
                </dl>
              )}
            </Card>
          );
        })}
      </div>

      <details className="rounded-lg border border-slate-200 bg-white p-3">
        <summary className="cursor-pointer text-sm font-medium text-slate-600">원자료 (전체 도메인 표)</summary>
        <div className="mt-3 grid gap-4 md:grid-cols-2 lg:grid-cols-4">
          {ex.data && Object.entries(ex.data).filter(([g]) => g !== "yyyymm").map(([g, obj]) => (
            <div key={g}>
              <p className="mb-1 text-xs font-semibold text-slate-500">{g}</p>
              <dl className="space-y-1 text-sm">
                {Object.entries(obj as Record<string, Val>).map(([k, v]) => (
                  <div key={k} className="flex items-center justify-between gap-2">
                    <dt className="text-slate-500">{k.replace(/_/g, " ")}</dt>
                    <dd className="font-medium">
                      {typeof v === "boolean" ? <Badge tone={v ? "green" : "red"}>{v ? "균형" : "불균형"}</Badge>
                        : v === null ? "—" : v.toLocaleString()}</dd>
                  </div>
                ))}
              </dl>
            </div>
          ))}
        </div>
      </details>
      {ex.isFetching && <p className="text-sm text-slate-400">불러오는 중…</p>}

      {drill && <DrillModal widgetId={drill} ym={ym} onClose={() => setDrill(null)} />}
    </div>
  );
}
