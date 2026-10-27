"use client";
/** 재무 — 진료과별 손익·원가 배부 스냅샷 (FR-50 확장): 보험수익 − 배부원가 − 삭감손실. */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { wonOrDash as won } from "@/lib/format";
import { Badge, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface PL { dept_code: string; department: string | null; revenue: number;
  allocated_cost: number; deduction_loss: number; profit: number; margin: number | null }
interface Snap { department_id: number; department: string | null; labor: number;
  material: number; overhead: number; total_cost: number }


export default function CostingPlPage() {
  const qc = useQueryClient();
  const [ym, setYm] = useState(monthLocal());
  const [material, setMaterial] = useState("0");
  const [overhead, setOverhead] = useState("0");
  const toast = useToast();

  const pl = useQuery({ queryKey: ["dept-pl", ym], queryFn: () =>
    api<{ items: PL[] }>(`/costing/department-pl?yyyymm=${ym}`) });
  const snap = useQuery({ queryKey: ["cost-snap", ym], queryFn: () =>
    api<{ items: Snap[] }>(`/costing/snapshot?yyyymm=${ym}`) });
  const ms = useQuery({ queryKey: ["cost-ms", ym], queryFn: () =>
    api<{ support_total: number; items: { department: string | null; code: string | null;
      direct_cost: number; allocated_support: number; total_cost: number; headcount: number }[] }>(
      `/costing/multistage?yyyymm=${ym}`) });

  const save = useMutation({
    mutationFn: () => api<{ reconciled: boolean; total_allocated: number }>("/costing/snapshot", {
      method: "POST", body: JSON.stringify({ yyyymm: ym, material_amount: Number(material) || 0,
        overhead_amount: Number(overhead) || 0 }) }),
    onSuccess: (d) => { toast.ok(`스냅샷 저장 — 총배부 ${won(d.total_allocated)} · 정합 ${d.reconciled ? "✓" : "✗"}`);
      qc.invalidateQueries({ queryKey: ["cost-snap"] }); qc.invalidateQueries({ queryKey: ["dept-pl"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — 진료과별 손익·원가 배부 스냅샷</h1>
      <QueryErrorBanner queries={[pl, snap, ms]} />

      <Card title="배부 스냅샷 저장(마감 기준)" action={
        <span className="text-[11px] text-slate-400">인건비 자동 · 재료/간접 입력</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={ym} onChange={(e) => setYm(e.target.value)} placeholder="YYYY-MM"
            className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input value={material} onChange={(e) => setMaterial(e.target.value)} placeholder="재료비"
            className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input value={overhead} onChange={(e) => setOverhead(e.target.value)} placeholder="간접비"
            className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <button onClick={() => save.mutate()} disabled={save.isPending}
            className="rounded bg-blue-600 px-4 py-1.5 text-sm text-white disabled:opacity-50">스냅샷 저장</button>
        </div>
      </Card>

      <Card title={`진료과별 손익 (${ym})`} action={
        <span className="text-[11px] text-slate-400">보험 결정수익 − 배부원가 − 삭감손실</span>}>
        <Table headers={["진료과", "보험수익", "배부원가", "삭감손실", "손익", "이익률"]}
          rows={(pl.data?.items ?? []).map((p) => [
            p.department ?? p.dept_code, won(p.revenue), won(p.allocated_cost), won(p.deduction_loss),
            <b key="p" className={p.profit < 0 ? "text-red-600" : "text-emerald-700"}>{won(p.profit)}</b>,
            p.margin == null ? "-"
              : <Badge key="m" tone={p.margin < 0 ? "red" : p.margin < 10 ? "amber" : "green"}>{p.margin}%</Badge>,
          ])} empty={pl.isFetching ? "불러오는 중…" : "청구 데이터 없음(진료월 기준)"} />
      </Card>

      <Card title={`2단계 원가배부 — 지원부서→진료부서 (${ym})`} action={
        <span className="text-[11px] text-slate-400">지원부서 원가 {won(ms.data?.support_total ?? 0)} 인원수 배부</span>}>
        <Table headers={["진료부서", "직접원가", "배부 지원원가", "총원가", "인원"]}
          rows={(ms.data?.items ?? []).map((r) => [
            r.department ?? r.code, won(r.direct_cost),
            <span key="a" className="text-blue-700">{won(r.allocated_support)}</span>,
            <b key="t">{won(r.total_cost)}</b>, r.headcount,
          ])} empty={ms.isFetching ? "불러오는 중…" : "스냅샷 저장 후 표시"} />
      </Card>

      <Card title={`원가 배부 스냅샷 (${ym})`}>
        <Table headers={["부서", "인건비", "재료비", "간접비", "총원가"]}
          rows={(snap.data?.items ?? []).map((s) => [
            s.department ?? s.department_id, won(s.labor), won(s.material), won(s.overhead),
            <b key="t">{won(s.total_cost)}</b>,
          ])} empty="스냅샷 미저장 — 위에서 저장하세요" />
      </Card>
    </div>
  );
}
