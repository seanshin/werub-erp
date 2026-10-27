"use client";
/** 인사 — 퇴직급여충당부채 월별 적립(전입액 → 비용·부채 전표). */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { won } from "@/lib/format";
import { Button, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface Item { employee_id: number; employee_name: string | null; employee_no: string | null; avg_wage: number; estimate: number;
  increment: number; cumulative: number }
interface Prov { period: string; total_estimate: number; total_increment: number; items: Item[] }
interface Dbo { dbo: number; nominal_estimate: number; provision_balance: number; remeasurement: number;
  discount_rate: number; salary_growth: number; retirement_age: number; employees_no_birth: number }


export default function RetirementPage() {
  const qc = useQueryClient();
  const [ym, setYm] = useState(() => monthLocal());
  const toast = useToast();
  const prov = useQuery({ queryKey: ["retire", ym], queryFn: () =>
    api<Prov>(`/hr/retirement/provisions/${ym}`) });
  const dbo = useQuery({ queryKey: ["dbo", ym], enabled: /^\d{4}-\d{2}$/.test(ym),
    queryFn: () => api<Dbo>(`/hr/retirement/dbo/preview/${ym}`) });
  const postDbo = useMutation({
    mutationFn: () => api(`/hr/retirement/dbo/post/${ym}`, { method: "POST" }),
    onSuccess: (d: unknown) => { const r = d as { posted: boolean };
      toast.ok(r.posted ? "보험수리적 재측정(OCI) 전기 완료" : "재측정 조정 없음");
      qc.invalidateQueries({ queryKey: ["dbo"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "재측정 전기 실패"),
  });

  const accrue = useMutation({
    mutationFn: () => api<{ employees: number; total_increment: number;
      db_provision?: number; dc_contribution?: number; voucher_id: number | null }>(
      `/hr/retirement/accrue/${ym}`, { method: "POST" }),
    onSuccess: (d) => { toast.ok(`${ym} 적립 — 직원 ${d.employees}명 · DB충당 ${won(d.db_provision ?? d.total_increment)} · DC부담금 ${won(d.dc_contribution ?? 0)}${d.voucher_id ? ` (전표 #${d.voucher_id})` : ""}`);
      qc.invalidateQueries({ queryKey: ["retire"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "적립 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">인사 — 퇴직급여충당부채 (월별 적립)</h1>
      <QueryErrorBanner queries={[prov, dbo]} />

      <Card title="월별 충당 전입" action={
        <span className="text-[11px] text-slate-400">추계액 증가분 → 퇴직급여비용/충당부채 전표</span>}>
        <div className="flex items-center gap-2">
          <input value={ym} onChange={(e) => setYm(e.target.value)} placeholder="YYYY-MM"
            className="w-36 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button disabled={accrue.isPending}
            onClick={() => /^\d{4}-\d{2}$/.test(ym) ? accrue.mutate() : toast.err("YYYY-MM 형식으로 입력하세요")}>충당 전입</Button>
        </div>
      </Card>

      <Card title="보험수리적 평가 (DBO, K-IFRS 1019)" action={
        <span className="text-[11px] text-slate-400">DBO=명목추계×((1+임금상승)/(1+할인율))^잔여근속 · 재측정=OCI</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <Button variant="neutral" onClick={() => dbo.refetch()}>DBO 미리보기</Button>
          <Button onClick={() => postDbo.mutate()} disabled={!dbo.data?.remeasurement || postDbo.isPending}>재측정 전기(OCI)</Button>
          {dbo.data && <span className="text-sm text-slate-600">할인 {(dbo.data.discount_rate * 100).toFixed(1)}%·임금상승 {(dbo.data.salary_growth * 100).toFixed(1)}%·정년 {dbo.data.retirement_age}</span>}
        </div>
        {dbo.data && (
          <p className="mt-2 text-sm text-slate-600">DBO(보험수리) <b>{won(dbo.data.dbo)}</b> · 명목추계 {won(dbo.data.nominal_estimate)} ·
            재측정(OCI=DBO−명목) <b className={dbo.data.remeasurement ? "text-blue-600" : "text-emerald-600"}>{won(dbo.data.remeasurement)}</b>
            {dbo.data.employees_no_birth > 0 && <span className="ml-1 text-amber-600">· 생년 미상 {dbo.data.employees_no_birth}명(명목 유지)</span>}</p>
        )}
        <p className="mt-1 text-[11px] text-slate-400">간이 PUC — 정상원가는 월별 적립(P/L), 할인·임금상승 가정차이는 OCI. 완전 보험수리는 외부 평가.</p>
      </Card>

      <Card title={`충당 현황 (${ym})`} action={prov.data ?
        <span className="text-xs text-slate-500">
          추계 합계 {won(prov.data.total_estimate)} · 당월 전입 {won(prov.data.total_increment)}</span> : null}>
        <Table headers={["직원", "평균임금", "추계액", "당월 전입", "누적 충당"]}
          rows={(prov.data?.items ?? []).map((r) => [
            r.employee_name ? `${r.employee_name}(${r.employee_no})` : `#${r.employee_id}`,
            won(r.avg_wage), won(r.estimate),
            <span key="i" className="text-blue-700">{won(r.increment)}</span>,
            <b key="c">{won(r.cumulative)}</b>,
          ])} empty={prov.isFetching ? "불러오는 중…" : "적립 내역 없음 — 위에서 전입하세요"} />
      </Card>
    </div>
  );
}
