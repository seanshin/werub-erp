"use client";
/** 경영분석 BI (FR-19 확장) — 재무·인사·진료·보험 KPI + 재무비율 + 월별 추세.
 * UX: 숫자 대비 단위(만원·억원·명·%·배·건) 55% 축소(시각 위계)·만원/억원 자동 스케일·
 * 수익/순이익 전월比 델타·KPI 서사 배열(손익 흐름 → 재무상태). */
import { localISO } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Card, Table } from "@/components/ui";
import { Kpi, Num, scaleWon } from "@/components/kpi";
import { QueryErrorBanner } from "@/components/query-guard";
import { BarChart } from "@/components/charts";

interface Overview {
  재무: Record<string, number>; 인사: Record<string, number>;
  진료: Record<string, number>; 보험: Record<string, number>;
}
interface Trend { months: { month: string; revenue: number; expense: number; net: number }[] }
interface Labor { department: string | null; labor_cost: number; headcount: number }
interface DeptPL { labor_by_department: Labor[] }
interface Ratios { current_ratio: number | null; debt_ratio: number | null;
  equity_ratio: number | null; operating_margin: number | null;
  labor_cost_ratio: number | null; material_cost_ratio: number | null }

export default function AnalyticsPage() {
  const now = new Date();
  const [yyyymm, setYyyymm] = useState(localISO(now).slice(0, 7));
  const year = Number(yyyymm.slice(0, 4));

  const ov = useQuery({ queryKey: ["ov", yyyymm],
    queryFn: () => api<Overview>(`/analytics/overview?yyyymm=${yyyymm}`) });
  const pl = useQuery({ queryKey: ["dpl", yyyymm],
    queryFn: () => api<DeptPL>(`/analytics/department-pl?yyyymm=${yyyymm}`) });
  const tr = useQuery({ queryKey: ["tr", year],
    queryFn: () => api<Trend>(`/analytics/trend?year=${year}`) });
  const fr = useQuery({ queryKey: ["fr", year],
    queryFn: () => api<Ratios>(`/analytics/financial-ratios?year=${year}`) });

  const f = ov.data?.재무 ?? {}; const h = ov.data?.인사 ?? {};
  const p = ov.data?.진료 ?? {}; const c = ov.data?.보험 ?? {};

  // 전월比 델타 — trend에서 당월/전월 비교(0 분모·데이터 없음은 미표시)
  const months = tr.data?.months ?? [];
  const mi = months.findIndex((m) => m.month === yyyymm);
  const deltaOf = (key: "revenue" | "net"): { pct: number; goodWhenUp?: boolean } | null => {
    if (mi < 1) return null;
    const cur = months[mi][key], prev = months[mi - 1][key];
    if (!prev) return null;
    return { pct: ((cur - prev) / Math.abs(prev)) * 100 };
  };

  const pctv = (v: number | null | undefined) =>
    v == null ? { value: "—", unit: "" } : { value: (v * 100).toFixed(1), unit: "%" };
  const multv = (v: number | null | undefined) =>
    v == null ? { value: "—", unit: "" } : { value: v.toFixed(2), unit: "배" };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">경영분석 (BI 대시보드)</h1>
        <QueryErrorBanner queries={[ov, pl, tr, fr]} />
        <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm" />
      </div>

      {/* 손익 흐름(수익→비용→순이익→순현금) → 재무상태(자산·부채·미수·미지급) 서사 배열 */}
      <Card title="재무 KPI">
        <div className="grid gap-2 sm:grid-cols-4">
          <Kpi label="수익" {...scaleWon(f["수익"])} tone="text-blue-600"
            delta={deltaOf("revenue")} />
          <Kpi label="비용" {...scaleWon(f["비용"])} tone="text-slate-700" />
          <Kpi label="당기순이익" {...scaleWon(f["당기순이익"])}
            tone={(f["당기순이익"] ?? 0) >= 0 ? "text-blue-600" : "text-red-600"}
            delta={deltaOf("net")} />
          <Kpi label="순현금" {...scaleWon(f["순현금"])} />
          <Kpi label="자산총계" {...scaleWon(f["자산총계"])} />
          <Kpi label="부채총계" {...scaleWon(f["부채총계"])} />
          <Kpi label="미수금 잔액" {...scaleWon(f["미수금잔액"])} tone="text-amber-600" />
          <Kpi label="미지급 잔액" {...scaleWon(f["미지급잔액"])} tone="text-amber-600" />
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="인사·진료·보험 KPI">
          <div className="grid gap-2 sm:grid-cols-3">
            <Kpi label="재직인원" value={`${h["재직인원"] ?? 0}`} unit="명" />
            <Kpi label="월 인건비" {...scaleWon(h["월인건비"])} />
            <Kpi label="인건비/수익" value={`${h["인건비_대_수익_%"] ?? 0}`} unit="%" />
            <Kpi label="환자수" value={(p["환자수"] ?? 0).toLocaleString()} unit="명" />
            <Kpi label="당월 접수" value={`${p["당월접수"] ?? 0}`} unit="건" />
            <Kpi label="예약" value={`${p["예약"] ?? 0}`} unit="건" />
            <Kpi label="삭감손실" {...scaleWon(c["삭감손실"])} tone="text-red-600" />
            <Kpi label="삭감률" value={`${c["삭감률_%"] ?? 0}`} unit="%" />
          </div>
        </Card>

        <Card title="진료과별 인건비">
          <Table headers={["진료과", "인건비", "인원"]}
            rows={(pl.data?.labor_by_department ?? []).map((d) => {
              const w = scaleWon(d.labor_cost);
              return [d.department ?? "-",
                <Num key="c" value={w.value} unit={w.unit} />,
                <Num key="h" value={String(d.headcount)} unit="명" />];
            })} />
        </Card>
      </div>

      <Card title={`재무비율 (${year})`}>
        <div className="grid gap-2 sm:grid-cols-3 lg:grid-cols-6">
          <Kpi label="유동비율" {...multv(fr.data?.current_ratio)} />
          <Kpi label="부채비율" {...pctv(fr.data?.debt_ratio)} tone="text-amber-600" />
          <Kpi label="자기자본비율" {...pctv(fr.data?.equity_ratio)} />
          <Kpi label="의료이익률" {...pctv(fr.data?.operating_margin)}
            tone={(fr.data?.operating_margin ?? 0) >= 0 ? "text-blue-600" : "text-red-600"} />
          <Kpi label="인건비율" {...pctv(fr.data?.labor_cost_ratio)} />
          <Kpi label="재료비율" {...pctv(fr.data?.material_cost_ratio)} />
        </div>
      </Card>

      <Card title={`월별 순이익 추세 (${year})`}>
        <BarChart format={(n) => { const w = scaleWon(n); return `${w.value}${w.unit}`; }}
          data={months.map((m) => ({ label: m.month.slice(5), value: m.net }))} />
      </Card>

      <Card title={`월별 손익 상세 (${year})`}>
        <Table headers={["월", "수익", "비용", "순이익"]}
          rows={months.filter((m) => m.revenue || m.expense).map((m) => {
            const r = scaleWon(m.revenue), e = scaleWon(m.expense), n = scaleWon(m.net);
            return [m.month,
              <Num key="r" value={r.value} unit={r.unit} />,
              <Num key="e" value={e.value} unit={e.unit} />,
              <Num key="n" value={n.value} unit={n.unit}
                tone={m.net >= 0 ? "text-blue-700" : "text-red-600"} />];
          })} empty="해당 연도 전표 없음" />
      </Card>
    </div>
  );
}
