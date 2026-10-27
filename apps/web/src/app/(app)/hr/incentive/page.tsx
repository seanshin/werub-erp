"use client";
/** 인사 — 의료진 성과급 대시보드 (P4-5). 의사별 실적·성과급 미리보기 + 산정 워크플로(등록→검토→승인→취소). */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { BarChart } from "@/components/charts";

interface Row {
  employee_id: number; name: string; job_category: string | null; net_revenue: number;
  basis: string | null; incentive_type: string; rate_pct: number | null;
  preview_amount: number; calc_id: number | null; calc_status: string | null;
  calc_amount: number | null;
}
interface Dash { period: string; rows: Row[];
  totals: { preview: number; approved: number; doctors: number } }

const STATUS_TONE: Record<string, "gray" | "amber" | "blue" | "green" | "red"> = {
  DRAFT: "gray", REVIEWED: "amber", APPROVED: "green", CANCELED: "red" };

export default function IncentivePage() {
  const qc = useQueryClient();
  const [period, setPeriod] = useState(() => monthLocal());
  const toast = useToast();
  const { data } = useQuery({
    queryKey: ["incentive-dash", period],
    queryFn: () => api<Dash>(`/hr/incentive/dashboard?period=${period}`),
  });

  const act = useMutation({
    mutationFn: ({ url }: { url: string }) => api(url, { method: "POST",
      body: url.includes("register") ? JSON.stringify({}) : undefined }),
    onSuccess: () => { toast.ok("처리 완료"); qc.invalidateQueries({ queryKey: ["incentive-dash", period] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const register = useMutation({
    mutationFn: (employee_id: number) => api(`/hr/incentive/register`, { method: "POST",
      body: JSON.stringify({ employee_id, period }) }),
    onSuccess: () => { toast.ok("산정 등록(DRAFT)"); qc.invalidateQueries({ queryKey: ["incentive-dash", period] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });

  const rows = data?.rows ?? [];
  const chart = rows.filter((r) => r.preview_amount > 0)
    .map((r) => ({ label: r.name, value: r.preview_amount }));

  const busy = register.isPending || act.isPending;   // 이중 제출 방지 — GL/급여 이중 전기 차단
  const actionFor = (r: Row) => {
    if (!r.calc_status) return <Button variant="secondary" disabled={busy} onClick={() => register.mutate(r.employee_id)}>산정 등록</Button>;
    if (r.calc_status === "DRAFT") return <Button variant="secondary" disabled={busy} onClick={() => act.mutate({ url: `/hr/incentive/${r.calc_id}/review` })}>검토</Button>;
    if (r.calc_status === "REVIEWED") return <Button disabled={busy} onClick={() => act.mutate({ url: `/hr/incentive/${r.calc_id}/approve` })}>승인</Button>;
    if (r.calc_status === "APPROVED") return <Button variant="secondary" disabled={busy} onClick={() => act.mutate({ url: `/hr/incentive/${r.calc_id}/cancel` })}>취소</Button>;
    return null;
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 의료진 성과급</h1>
        <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card title="대상 의료진"><p className="text-2xl font-bold">{data?.totals.doctors ?? 0}명</p></Card>
        <Card title="성과급 미리보기 합계">
          <p className="text-2xl font-bold text-blue-700">{(data?.totals.preview ?? 0).toLocaleString()}</p></Card>
        <Card title="승인 확정 합계">
          <p className="text-2xl font-bold text-emerald-700">{(data?.totals.approved ?? 0).toLocaleString()}</p></Card>
      </div>

      {chart.length > 0 && (
        <Card title="의사별 성과급(미리보기)"><BarChart data={chart} /></Card>)}

      <Card title="성과급 산정 현황 (등록→검토→승인)">
        <Table headers={["의사", "직종", "진료실적(net)", "기준", "산식", "배분율", "미리보기", "상태", "확정액", "처리"]}
          rows={rows.map((r) => [
            r.name, r.job_category ?? "-", r.net_revenue.toLocaleString(),
            <Badge key="b" tone={r.basis === "CLAIM" ? "blue" : "gray"}>{r.basis === "CLAIM" ? "청구" : "수납"}</Badge>,
            r.incentive_type, r.rate_pct != null ? `${r.rate_pct}%` : "-",
            r.preview_amount.toLocaleString(),
            r.calc_status
              ? <Badge key="s" tone={STATUS_TONE[r.calc_status]}>{r.calc_status}</Badge>
              : <span className="text-xs text-slate-400">미등록</span>,
            r.calc_amount != null ? r.calc_amount.toLocaleString() : "-",
            actionFor(r),
          ])} empty="ACTIVE 의료진 계약 없음 — 먼저 의료진 계약을 등록하세요" />
        <p className="mt-2 text-xs text-slate-400">
          실적 기준: 청구(발생주의) 우선, 청구 담당의 미채움 시 수납(본인부담). 승인 시 급여 명세(INCENTIVE)·미지급 전표 자동.
          검토자≠승인자 권장. 보수정보는 접속기록에 남습니다.
        </p>
      </Card>
    </div>
  );
}
