"use client";
/** 스프레드시트 → Recharts 시각화 패널.
 *  grid(2차원 값 배열)를 받아 범위(A1:D4)·차트유형을 선택해 렌더.
 *  규칙: 범위 첫 행=시리즈명(헤더), 첫 열=카테고리 라벨, 나머지 숫자 셀=값. */
import { useMemo, useState } from "react";
import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart,
  Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

export type Grid = (string | number | null)[][];

const PALETTE = ["#2563eb", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6",
                 "#06b6d4", "#f97316", "#84cc16"];
const TYPES = [
  { key: "bar", label: "막대" }, { key: "line", label: "선" },
  { key: "area", label: "영역" }, { key: "pie", label: "원형" },
] as const;
type ChartType = (typeof TYPES)[number]["key"];

/** "A1:D4" → {r0,c0,r1,c1} (0-base). 잘못된 형식은 null. */
export function parseRange(ref: string): { r0: number; c0: number; r1: number; c1: number } | null {
  const m = ref.trim().toUpperCase().match(/^([A-Z]+)(\d+):([A-Z]+)(\d+)$/);
  if (!m) return null;
  const col = (s: string) => s.split("").reduce((a, ch) => a * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  const [c0, r0, c1, r1] = [col(m[1]), +m[2] - 1, col(m[3]), +m[4] - 1];
  if (r1 < r0 || c1 < c0) return null;
  return { r0, c0, r1, c1 };
}

const num = (v: string | number | null): number | null => {
  if (v === null || v === "") return null;
  const n = typeof v === "number" ? v : Number(String(v).replace(/,/g, ""));
  return Number.isFinite(n) ? n : null;
};

/** grid의 사용 영역을 "A1:D4" 표기로. */
function autoRange(grid: Grid): string {
  const rows = grid.length;
  const cols = Math.max(0, ...grid.map((r) => r.length));
  if (!rows || !cols) return "";
  const colName = (c: number) => {
    let s = "";
    for (let n = c + 1; n > 0; n = Math.floor((n - 1) / 26)) {
      s = String.fromCharCode(65 + ((n - 1) % 26)) + s;
    }
    return s;
  };
  return `A1:${colName(cols - 1)}${rows}`;
}

/** 범위 → recharts rows + 시리즈 키. 첫 행=헤더, 첫 열=라벨. */
export function toChartData(grid: Grid, ref: string): { rows: Record<string, string | number>[]; series: string[] } {
  const r = parseRange(ref);
  if (!r) return { rows: [], series: [] };
  const at = (ri: number, ci: number) => grid[ri]?.[ci] ?? null;
  const series: string[] = [];
  for (let c = r.c0 + 1; c <= r.c1; c++) {
    series.push(String(at(r.r0, c) ?? `열${c + 1}`));
  }
  const rows: Record<string, string | number>[] = [];
  for (let ri = r.r0 + 1; ri <= r.r1; ri++) {
    const row: Record<string, string | number> = { name: String(at(ri, r.c0) ?? `행${ri + 1}`) };
    let has = false;
    for (let c = r.c0 + 1; c <= r.c1; c++) {
      const v = num(at(ri, c));
      if (v !== null) { row[series[c - r.c0 - 1]] = v; has = true; }
    }
    if (has) rows.push(row);
  }
  return { rows, series };
}

export default function SheetChart({ grid }: { grid: Grid }) {
  const [type, setType] = useState<ChartType>("bar");
  const [refInput, setRefInput] = useState("");
  const ref = refInput.trim() || autoRange(grid);
  const { rows, series } = useMemo(() => toChartData(grid, ref), [grid, ref]);
  const input = "rounded-md border border-slate-300 px-3 py-1.5 text-sm";

  const body = () => {
    if (!rows.length || !series.length) {
      return <div className="flex h-64 items-center justify-center text-sm text-slate-400">
        표시할 숫자 데이터가 없습니다 — 범위(예: A1:D4)를 확인하세요</div>;
    }
    if (type === "pie") {
      const key = series[0];
      const data = rows.filter((r0) => typeof r0[key] === "number");
      return (
        <ResponsiveContainer width="100%" height={280}>
          <PieChart>
            <Pie data={data} dataKey={key} nameKey="name" outerRadius={100} label>
              {data.map((_, i) => <Cell key={i} fill={PALETTE[i % PALETTE.length]} />)}
            </Pie>
            <Tooltip /><Legend />
          </PieChart>
        </ResponsiveContainer>
      );
    }
    const Chart = type === "line" ? LineChart : type === "area" ? AreaChart : BarChart;
    return (
      <ResponsiveContainer width="100%" height={280}>
        <Chart data={rows}>
          <CartesianGrid strokeDasharray="3 3" stroke="#e2e8f0" />
          <XAxis dataKey="name" tick={{ fontSize: 12 }} />
          <YAxis tick={{ fontSize: 12 }} tickFormatter={(v: number) => v.toLocaleString()} />
          <Tooltip formatter={(v) => Number(v).toLocaleString()} />
          <Legend />
          {series.map((s, i) => {
            const color = PALETTE[i % PALETTE.length];
            if (type === "line") {
              return <Line key={s} dataKey={s} stroke={color} strokeWidth={2} dot={false} />;
            }
            if (type === "area") {
              return <Area key={s} dataKey={s} stroke={color} fill={color} fillOpacity={0.25} />;
            }
            return <Bar key={s} dataKey={s} fill={color} radius={[3, 3, 0, 0]} />;
          })}
        </Chart>
      </ResponsiveContainer>
    );
  };

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        {TYPES.map((t) => (
          <button key={t.key} onClick={() => setType(t.key)}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${type === t.key
              ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
            {t.label}
          </button>
        ))}
        <input value={refInput} onChange={(e) => setRefInput(e.target.value)}
          placeholder={`범위 (기본 ${ref || "전체"})`} className={`${input} w-44`} />
        <span className="text-[11px] text-slate-400">
          첫 행=시리즈명 · 첫 열=라벨 — 시트 편집 시 즉시 반영{type === "pie" ? " · 원형은 첫 시리즈만" : ""}
        </span>
      </div>
      {body()}
    </div>
  );
}
