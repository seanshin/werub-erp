"use client";
/** 감사·이상탐지 (ADR D-7) — Benford·중복전표·비정상 분개 + 검토완료 워크플로.
 *  전기완료(POSTED) 전표 분석 · 결과는 검토 후보이며 자동 판정이 아님. 경영진(_FIN) 권한. */
import { localISO } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { Badge, Card } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { AbnormalPanel, BenfordPanel, DuplicatePanel } from "./panels";
import { man } from "@/lib/format";

interface Summary {
  benford: { verdict: string; mad: number; chi_square_exceeds: boolean; sample_size: number };
  duplicates: { cluster_count: number; open_count: number; duplicate_amount: number };
  abnormal: { flagged_count: number; open_count: number; by_reason: Record<string, number> };
  open_total: number;
}
const VERDICT_TONE: Record<string, "green" | "blue" | "amber" | "red"> = {
  "근접 부합": "green", "허용 가능": "blue", "한계": "amber", "부적합": "red",
};
const iso = (d: Date) => localISO(d);

function Kpi({ label, children, sub }: { label: string; children: React.ReactNode; sub?: string }) {
  return (
    <div className="rounded-lg border border-slate-200 bg-white p-3">
      <p className="text-xs text-slate-500">{label}</p>
      <div className="mt-0.5 text-lg font-bold text-slate-800">{children}</div>
      {sub && <p className="mt-0.5 text-[11px] text-slate-400">{sub}</p>}
    </div>
  );
}

export default function FinanceAuditPage() {
  const today = new Date();
  const year = today.getFullYear();
  const [from, setFrom] = useState(`${year}-01-01`);   // 기본: 당해연도 YTD
  const [to, setTo] = useState(iso(today));

  const presets: [string, () => void][] = [
    ["당월", () => { setFrom(`${year}-${String(today.getMonth() + 1).padStart(2, "0")}-01`); setTo(iso(today)); }],
    ["당분기", () => { setFrom(iso(new Date(year, Math.floor(today.getMonth() / 3) * 3, 1))); setTo(iso(today)); }],
    ["당해", () => { setFrom(`${year}-01-01`); setTo(iso(today)); }],
  ];

  const s = useQuery({ queryKey: ["audit", "summary", from, to],
    queryFn: () => api<Summary>(`/analytics/audit/summary?date_from=${from}&date_to=${to}`) });
  const d = s.data;
  const reasons = d?.abnormal.by_reason ?? {};
  const reasonSub = ["weekend", "offhours", "round", "below_threshold"]
    .map((k) => reasons[k]).filter(Boolean).length
    ? `주말 ${reasons.weekend ?? 0}·심야 ${reasons.offhours ?? 0}·라운드 ${reasons.round ?? 0}·한도 ${reasons.below_threshold ?? 0}`
    : "—";

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[s]} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">감사·이상탐지</h1>
        <div className="flex flex-wrap items-center gap-2">
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <span className="text-slate-400">~</span>
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          {presets.map(([l, fn]) => (
            <button key={l} onClick={fn}
              className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-600 hover:bg-slate-50">{l}</button>
          ))}
        </div>
      </div>
      <p className="text-xs text-slate-500">전기완료(POSTED) 전표 분석 · 결과는 <b>검토 후보</b>이며 자동 판정이 아닙니다.</p>

      <Card title="요약">
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Kpi label="Benford 부합" sub={d ? `MAD ${d.benford.mad} · 표본 ${d.benford.sample_size.toLocaleString()}` : undefined}>
            {d ? <Badge tone={VERDICT_TONE[d.benford.verdict] ?? "gray"}>{d.benford.verdict}</Badge> : "—"}
          </Kpi>
          <Kpi label="중복전표 후보" sub={d ? `의심 누적 ${man(d.duplicates.duplicate_amount)}` : undefined}>
            <span className={d && d.duplicates.cluster_count ? "text-amber-600" : ""}>{d?.duplicates.cluster_count ?? 0}건</span>
          </Kpi>
          <Kpi label="비정상 분개" sub={reasonSub}>
            <span className={d && d.abnormal.flagged_count ? "text-amber-600" : ""}>{d?.abnormal.flagged_count ?? 0}건</span>
          </Kpi>
          <Kpi label="미검토 후보 합계" sub="검토완료 제외 · Benford 제외">
            <span className="text-slate-800">{d?.open_total ?? 0}건</span>
          </Kpi>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <BenfordPanel from={from} to={to} />
        <DuplicatePanel from={from} to={to} />
      </div>
      <AbnormalPanel from={from} to={to} />
    </div>
  );
}
