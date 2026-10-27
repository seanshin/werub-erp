"use client";
/** 재무 — 자금일보: 일자 기준 현금성 계정별 기초→입금→출금→기말 + 출처별 유출입(일일 시재 대사). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";
import { Card, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";

interface AcctRow { account_code: string; opening: number; inflow: number; outflow: number; closing: number }
interface DailyCash {
  on: string; accounts: AcctRow[]; by_module: Record<string, { inflow: number; outflow: number }>;
  opening: number; inflow: number; outflow: number; closing: number;
}

export default function DailyCashPage() {
  const [on, setOn] = useState(todayLocal());
  const dc = useQuery({ queryKey: ["daily-cash", on], queryFn: () =>
    api<DailyCash>(`/finance/daily-cash?on=${on}`) });

  const modules = Object.entries(dc.data?.by_module ?? {});

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 자금일보</h1>
      {dc.isError && <QueryError error={dc.error} onRetry={() => dc.refetch()} />}

      <Card title="조회 일자" action={
        <span className="text-[11px] text-slate-400">현금성 계정(1110 + 활성 법인계좌) · 기초=전일까지 순증</span>}>
        <input type="date" value={on} onChange={(e) => setOn(e.target.value)}
          className="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
      </Card>

      <div className="grid gap-4 lg:grid-cols-4">
        <Card title="기초 잔액"><p className="text-2xl font-bold">{won(dc.data?.opening ?? 0)}</p></Card>
        <Card title="당일 입금"><p className="text-2xl font-bold text-emerald-700">{won(dc.data?.inflow ?? 0)}</p></Card>
        <Card title="당일 출금"><p className="text-2xl font-bold text-red-600">{won(dc.data?.outflow ?? 0)}</p></Card>
        <Card title="기말 잔액"><p className="text-2xl font-bold">{won(dc.data?.closing ?? 0)}</p></Card>
      </div>

      <Card title="계정별 자금 현황">
        <Table headers={["계정코드", "기초", "입금", "출금", "기말"]}
          rows={(dc.data?.accounts ?? []).map((a) => [
            a.account_code, won(a.opening),
            <span key="i" className="text-emerald-700">{won(a.inflow)}</span>,
            <span key="o" className="text-red-600">{won(a.outflow)}</span>,
            <b key="c">{won(a.closing)}</b>,
          ])} empty={dc.isFetching ? "불러오는 중…" : "현금성 계정 없음"} />
      </Card>

      <Card title="출처별 당일 유출입" action={
        <span className="text-[11px] text-slate-400">전표 출처 모듈 기준</span>}>
        <Table headers={["출처", "입금", "출금"]}
          rows={modules.map(([mod, v]) => [
            mod,
            <span key="i" className="text-emerald-700">{won(v.inflow)}</span>,
            <span key="o" className="text-red-600">{won(v.outflow)}</span>,
          ])} empty={dc.isFetching ? "불러오는 중…" : "당일 유출입 없음"} />
      </Card>
    </div>
  );
}
