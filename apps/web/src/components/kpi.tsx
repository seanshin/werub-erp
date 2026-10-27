"use client";
/** KPI 표시 토큰 — 숫자는 크게, 단위(명·원·%·배·건)는 ~50% 작게(시각 위계). 경영분석 등 지표 화면 공용.
 * 통화는 만원/억원 자동 스케일(scaleWon). delta=전기 대비(상승·하락 화살표, good 기준 색). */
import { ReactNode } from "react";

/** 원 → {value, unit} 자동 스케일: <1만 → 원, <1억 → 만원, ≥1억 → 억원(소수1). */
export function scaleWon(n: number | null | undefined): { value: string; unit: string } {
  const v = Number.isFinite(n as number) ? (n as number) : 0;   // NaN/undefined 방어
  const abs = Math.abs(v);
  const eok = (x: number) => x.toLocaleString("ko-KR", {
    minimumFractionDigits: 1, maximumFractionDigits: 1 });
  if (abs >= 100_000_000) return { value: eok(v / 100_000_000), unit: "억원" };
  const man = Math.round(v / 10_000);
  // 반올림 결과가 억 경계(±10,000만원=1억)에 닿으면 억원으로 승격 — 99,997,000이 "10,000만원"으로
  // 표기되던 억 경계 불일치 해소.
  if (Math.abs(man) >= 10_000) return { value: eok(man / 10_000), unit: "억원" };
  // 만원 미만(|v|<10,000)은 원 단위 — "0만원"/"-0만원"(음수 0) 정보손실·회귀 방지.
  if (abs > 0 && abs < 10_000) return { value: v.toLocaleString("ko-KR"), unit: "원" };
  return { value: (Object.is(man, -0) ? 0 : man).toLocaleString("ko-KR"), unit: "만원" };
}

export function Kpi({ label, value, unit, tone, delta, hint }: {
  label: string;
  value: string;
  unit?: string;               // 단위 — 숫자의 55% 크기·저채도(시각 위계)
  tone?: string;               // 숫자 색 클래스(기본 slate-800)
  delta?: { pct: number; goodWhenUp?: boolean } | null;   // 전기 대비 %(화살표·색)
  hint?: ReactNode;            // 보조 텍스트(작게)
}) {
  const up = (delta?.pct ?? 0) >= 0;
  const good = delta == null ? null : (delta.goodWhenUp ?? true) === up;
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-3.5 transition hover:shadow-sm">
      <div className="flex items-baseline justify-between gap-1">
        <p className="text-[11px] font-medium tracking-wide text-slate-400">{label}</p>
        {delta != null && (
          <span className={`text-[10px] font-semibold tabular-nums ${
            good ? "text-emerald-600" : "text-red-500"}`}>
            {up ? "▲" : "▼"} {Math.abs(delta.pct).toFixed(1)}%
          </span>
        )}
      </div>
      <p className={`mt-1 text-right tabular-nums leading-none ${tone ?? "text-slate-800"}`}>
        <span className="text-[22px] font-bold">{value}</span>
        {unit && <span className="ml-0.5 align-baseline text-xs font-medium text-slate-400">{unit}</span>}
      </p>
      {hint && <p className="mt-1 text-right text-[10px] text-slate-400">{hint}</p>}
    </div>
  );
}

/** 표 셀 등 인라인 숫자+작은 단위. */
export function Num({ value, unit, tone }: { value: string; unit?: string; tone?: string }) {
  return (
    <span className={`tabular-nums ${tone ?? ""}`}>
      {value}{unit && <span className="ml-0.5 text-[0.7em] text-slate-400">{unit}</span>}
    </span>
  );
}
