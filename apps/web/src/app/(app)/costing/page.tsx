"use client";
/** 재무 — ABC 원가 분석 (FR-50): 진료과별 인건비·삭감손실. */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { won } from "@/lib/format";
import { Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";

interface Labor { department_id: number; department: string | null;
                  labor_cost: number; headcount: number }
interface Loss { dept_code: string; claimed: number; decided: number; deduction_loss: number }
interface ByDept { yyyymm: string; labor_by_department: Labor[];
                   deduction_loss_by_department: Loss[] }
interface Summary { yyyymm: string; labor_total: number; deduction_loss_total: number }

export default function CostingPage() {
  const [yyyymm, setYyyymm] = useState(monthLocal());

  const summary = useQuery({
    queryKey: ["costing-summary", yyyymm],
    queryFn: () => api<Summary>(`/costing/summary?yyyymm=${yyyymm}`),
  });
  const byDept = useQuery({
    queryKey: ["costing-by-dept", yyyymm],
    queryFn: () => api<ByDept>(`/costing/by-department?yyyymm=${yyyymm}`),
  });


  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — ABC 원가 분석</h1>
        <QueryErrorBanner queries={[summary, byDept]} />
        <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-sm" />
      </div>

      {(summary.isError || byDept.isError) && (
        <p className="text-sm text-red-600">원가 데이터를 불러오지 못했습니다 (재무 권한 확인)</p>)}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card title="인건비 총계">
          <p className="text-2xl font-bold text-slate-800">
            {won(summary.data?.labor_total ?? 0)}</p>
        </Card>
        <Card title="삭감 손실 총계">
          <p className="text-2xl font-bold text-red-600">
            {won(summary.data?.deduction_loss_total ?? 0)}</p>
        </Card>
      </div>

      <Card title={`진료과별 인건비 (${yyyymm})`}>
        <Table headers={["진료과", "인건비", "인원", "1인당"]}
          rows={(byDept.data?.labor_by_department ?? []).map((d) => [
            d.department ?? `#${d.department_id}`,
            won(d.labor_cost), d.headcount,
            won(d.headcount ? Math.round(d.labor_cost / d.headcount) : 0),
          ])} />
      </Card>

      <Card title={`진료과별 삭감 손실 (청구 ${yyyymm})`}>
        <Table headers={["진료과(코드)", "청구액", "결정액", "삭감손실"]}
          rows={(byDept.data?.deduction_loss_by_department ?? []).map((d) => [
            d.dept_code, won(d.claimed), won(d.decided), won(d.deduction_loss)])} />
      </Card>
    </div>
  );
}
