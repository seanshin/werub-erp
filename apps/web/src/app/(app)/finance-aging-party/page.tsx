"use client";
/** 재무 — 거래처별 연령분석: 미결제 채무(AP)/채권(AR)을 (거래처 × 경과일 버킷) 매트릭스로. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";
import { Card, Table, Button } from "@/components/ui";
import { QueryError } from "@/components/query-guard";

type Bucket = "current" | "1-30" | "31-60" | "61-90" | "90+";
interface Party {
  party: string; party_ref: string | null; buckets: Record<Bucket, number>;
  total: number; next_due: string | null;
}
interface Aging { as_of: string; kind: string; parties: Party[]; total: number }

export default function AgingByPartyPage() {
  const [asOf, setAsOf] = useState(todayLocal());
  const [kind, setKind] = useState<"AP" | "AR">("AP");
  const ag = useQuery({ queryKey: ["aging-party", asOf, kind], queryFn: () =>
    api<Aging>(`/finance/aging-by-party?as_of=${asOf}&kind=${kind}`) });

  const label = kind === "AP" ? "채무(미지급금)" : "채권(미수금)";
  const overdue = (p: Party) => p.total - p.buckets.current;   // 연체(current 제외)
  // 경과일은 **서버가 준 as_of** 기준 — 렌더 중 Date.now()는 React 컴파일러가 impure로 막고,
  // 클라이언트 시계로 계산하면 서버 버킷 판정과 어긋난다.
  const agedDays = (due: string) => {
    const base = ag.data?.as_of;
    if (!base) return 0;
    return Math.round((new Date(base).getTime() - new Date(due).getTime()) / 864e5);
  };

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 거래처별 연령분석</h1>
      {ag.isError && <QueryError error={ag.error} onRetry={() => ag.refetch()} />}

      <Card title="조회 조건" action={
        <span className="text-[11px] text-slate-400">미결제 {label} · 경과일(due_on 기준) 버킷</span>}>
        <div className="flex items-center gap-2">
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button variant={kind === "AP" ? "primary" : "secondary"} size="sm"
            onClick={() => setKind("AP")}>채무 (AP)</Button>
          <Button variant={kind === "AR" ? "primary" : "secondary"} size="sm"
            onClick={() => setKind("AR")}>채권 (AR)</Button>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title={`총 미결제 ${label}`}><p className="text-2xl font-bold">{won(ag.data?.total ?? 0)}</p></Card>
        <Card title="거래처 수"><p className="text-2xl font-bold">{(ag.data?.parties ?? []).length}곳</p></Card>
      </div>

      <Card title={`거래처 × 경과일 매트릭스 (${label})`}>
        <Table headers={["거래처", "정상", "1-30일", "31-60일", "61-90일", "90일+", "총잔액", "최고령 기한"]}
          rows={(ag.data?.parties ?? []).map((p) => [
            p.party,
            won(p.buckets.current),
            <span key="b1" className={p.buckets["1-30"] > 0 ? "text-amber-700" : ""}>{won(p.buckets["1-30"])}</span>,
            <span key="b2" className={p.buckets["31-60"] > 0 ? "text-amber-700" : ""}>{won(p.buckets["31-60"])}</span>,
            <span key="b3" className={p.buckets["61-90"] > 0 ? "text-red-600" : ""}>{won(p.buckets["61-90"])}</span>,
            <span key="b4" className={p.buckets["90+"] > 0 ? "text-red-700 font-semibold" : ""}>{won(p.buckets["90+"])}</span>,
            <b key="t" className={overdue(p) > 0 ? "text-red-600" : ""}>{won(p.total)}</b>,
            // 서버 next_due = min(due_on) — 가장 **이른** 기한(대개 가장 오래 연체된 건)이다.
            // 구 헤더 "최근 예정일"은 정반대 의미라 독촉 우선순위를 거꾸로 읽게 했다.
            p.next_due
              ? <span key="d" className={agedDays(p.next_due) > 0 ? "text-red-600" : ""}>
                  {p.next_due}{agedDays(p.next_due) > 0 ? ` (D+${agedDays(p.next_due)})` : ""}</span>
              : "-",
          ])} empty={ag.isFetching ? "불러오는 중…" : "미결제 내역 없음"} />
      </Card>
    </div>
  );
}
