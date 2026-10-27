"use client";
/** 재무 — 검진권 운영 현황(집계). his.voucher 전표 lineType별 발급/사용/환급/정산 +
 *  미사용 발급 잔액(선수금 2140). 개별 검진권 serial 단위 상태는 HIS 소유(ERP 미저장). */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Card, Input, Table } from "@/components/ui";
import { BarChart } from "@/components/charts";

interface LineAgg { line_type: string; count: number; amount: number }
interface Metric { amount: number; count: number }
interface MonthPt { month: string; issued: number; redeemed: number }
interface VoucherStatus {
  range: { from: string; to: string };
  issued: Metric; redeemed: Metric; refunded: Metric; settled: Metric;
  unredeemed_krw: number;
  by_line_type: LineAgg[];
  settlement_callbacks: Record<string, number>;
  monthly_trend: MonthPt[];
}

const LT_KO: Record<string, string> = {
  purchase: "발급(매입)", sub_purchase: "하위배분", resale: "재판매", redeem: "사용(검진)",
  topup: "액면초과 추가", refund: "환급", commission: "수수료", settlement: "정산 지급",
};
const CB_TONE: Record<string, "gray" | "green" | "amber" | "red"> = {
  SENT: "green", QUEUED: "amber", FAILED: "amber", DLQ: "red",
};

export default function VoucherStatusPage() {
  const y = new Date().getFullYear();
  const [from, setFrom] = useState(`${y}-01-01`);
  const [to, setTo] = useState(todayLocal());
  const { data, isError } = useQuery({
    queryKey: ["voucher-status", from, to],
    queryFn: () => api<VoucherStatus>(`/analytics/voucher?date_from=${from}&date_to=${to}`),
  });

  const kpi = (label: string, m?: Metric, tone = "text-slate-800") => (
    <Card title={label}>
      <p className={`text-2xl font-bold ${tone}`}>{won(m?.amount ?? 0)}</p>
      <p className="mt-1 text-xs text-slate-400">{m?.count ?? 0}건</p>
    </Card>
  );

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 검진권 현황</h1>
      <p className="text-xs text-slate-500">
        검진권 거래의 회계 집계입니다(발급=병원→딜러 선결제 매입). 개별 검진권 serial 단위
        상태(발급/사용/환급)는 HIS 소유로 ERP엔 저장되지 않습니다.</p>
      <div className="flex items-end gap-2">
        <Input label="시작일" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
        <Input label="종료일" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
      </div>
      {isError && <p className="text-sm text-red-600">현황 조회 권한이 없습니다 (DIRECTOR/ADMIN)</p>}

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {kpi("발급(매입)", data?.issued)}
        {kpi("사용(검진)", data?.redeemed, "text-emerald-700")}
        {kpi("환급", data?.refunded, "text-amber-700")}
        <Card title="미사용 발급 잔액">
          <p className="text-2xl font-bold text-blue-700">{won(data?.unredeemed_krw ?? 0)}</p>
          <p className="mt-1 text-xs text-slate-400">선수금(2140) — 발급됐으나 미검진</p>
        </Card>
      </div>

      <Card title="월별 추이 — 발급(매입) vs 사용(검진)">
        {(data?.monthly_trend?.length ?? 0) === 0
          ? <p className="py-6 text-center text-sm text-slate-400">데이터 없음</p>
          : (
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <p className="mb-1 text-xs font-medium text-slate-500">발급(매입)</p>
                <BarChart data={(data?.monthly_trend ?? []).map((m) => ({
                  label: m.month.slice(2), value: m.issued, color: "#3b82f6" }))} />
              </div>
              <div>
                <p className="mb-1 text-xs font-medium text-slate-500">사용(검진)</p>
                <BarChart data={(data?.monthly_trend ?? []).map((m) => ({
                  label: m.month.slice(2), value: m.redeemed, color: "#10b981" }))} />
              </div>
            </div>
          )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="거래유형별 집계">
          <Table headers={["유형", "건수", "금액"]}
            rows={(data?.by_line_type ?? []).map((r) => [
              LT_KO[r.line_type] ?? r.line_type, `${r.count}건`, won(r.amount)])}
            empty="거래 없음" />
        </Card>
        <Card title="정산 지급 회신(콜백) 상태">
          <div className="flex flex-wrap gap-2">
            {Object.entries(data?.settlement_callbacks ?? {}).map(([s, c]) => (
              <Badge key={s} tone={CB_TONE[s] ?? "gray"}>{s} {c}</Badge>
            ))}
            {!data || Object.keys(data.settlement_callbacks).length === 0
              ? <span className="text-sm text-slate-400">정산 콜백 없음</span> : null}
          </div>
          <p className="mt-3 text-xs text-slate-400">
            SENT=HIS 반영 완료 · QUEUED/FAILED=재시도 대기 · DLQ=운영 확인 필요.</p>
        </Card>
      </div>
    </div>
  );
}
