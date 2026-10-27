"use client";
/** 재무 — 자금수지 예측: 현금잔액 + AR 수금예정·AP 지급예정으로 월별 포지션 추정. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";
import { Card, Table } from "@/components/ui";
import { QueryError } from "@/components/query-guard";

interface Row { label: string; inflow: number; outflow: number; net: number; balance: number }
interface Forecast { as_of: string; opening_cash: number; min_balance: number; projection: Row[] }


export default function CashForecastPage() {
  const [asOf, setAsOf] = useState(todayLocal());
  const [months, setMonths] = useState("6");
  const fc = useQuery({ queryKey: ["cash-fc", asOf, months], queryFn: () =>
    api<Forecast>(`/finance/cash-forecast?as_of=${asOf}&months=${Number(months) || 6}`) });

  const neg = (fc.data?.min_balance ?? 0) < 0;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 자금수지 예측</h1>
      {fc.isError && <QueryError error={fc.error} onRetry={() => fc.refetch()} />}

      <Card title="조회 조건" action={
        <span className="text-[11px] text-slate-400">미수금 수금예정·미지급금 지급예정(due) 기반</span>}>
        <div className="flex items-center gap-2">
          <input type="date" value={asOf} onChange={(e) => setAsOf(e.target.value)}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input value={months} onChange={(e) => setMonths(e.target.value)} placeholder="개월"
            className="w-20 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="기초 현금잔액"><p className="text-2xl font-bold">{won(fc.data?.opening_cash ?? 0)}</p></Card>
        <Card title="예측 최저 잔액">
          <p className={`text-2xl font-bold ${neg ? "text-red-600" : "text-emerald-700"}`}>{won(fc.data?.min_balance ?? 0)}</p>
          {neg && <p className="mt-1 text-xs text-red-600">⚠ 자금 부족 예상 — 수금 촉진/지급 조정 필요</p>}
        </Card>
      </div>

      <Card title="월별 자금 포지션">
        <Table headers={["기간", "수금예정", "지급예정", "순증감", "예상 잔액"]}
          rows={(fc.data?.projection ?? []).map((p) => [
            p.label, won(p.inflow), won(p.outflow),
            <span key="n" className={p.net < 0 ? "text-red-600" : "text-emerald-700"}>
              {p.net >= 0 ? "+" : ""}{won(p.net)}</span>,
            <b key="b" className={p.balance < 0 ? "text-red-600" : ""}>{won(p.balance)}</b>,
          ])} empty={fc.isFetching ? "불러오는 중…" : "데이터 없음"} />
      </Card>
    </div>
  );
}
