"use client";
/** 재무 — ABC 원가 배부 (FR-42): 원가풀(인건비 자동·재료/간접 입력)을 진료과에 동인 배부. */
import { monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { won } from "@/lib/format";
import { Button, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface Obj { object_id: number; department: string | null; pools: Record<string, number>; total: number }
interface Pool { name: string; amount: number; driver: string }
interface Result { yyyymm: string; objects: Obj[]; grand_total: number; pools: Pool[] }

export default function AbcPage() {
  const qc = useQueryClient();
  const [yyyymm, setYyyymm] = useState(monthLocal());
  const [material, setMaterial] = useState("0");
  const [overhead, setOverhead] = useState("0");
  const [dv, setDv] = useState({ department_id: "", driver: "CASES", value: "" });
  const toast = useToast();

  const r = useQuery({ queryKey: ["abc", yyyymm, material, overhead],
    queryFn: () => api<Result>(
      `/costing/abc?yyyymm=${yyyymm}&material_amount=${Number(material) || 0}&overhead_amount=${Number(overhead) || 0}`) });

  const saveDriver = useMutation({
    mutationFn: () => api("/costing/driver-values", { method: "PUT", body: JSON.stringify({
      period: yyyymm, department_id: Number(dv.department_id), driver: dv.driver,
      value: Number(dv.value) }) }),
    onSuccess: () => { toast.ok("동인값 저장 — 정밀 배부 반영"); qc.invalidateQueries({ queryKey: ["abc"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "저장 실패"),
  });

  const poolNames = (r.data?.pools ?? []).map((p) => p.name);

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재무 — ABC 원가 배부</h1>
      <QueryErrorBanner queries={[r]} />

      <Card title="원가풀 입력 (인건비는 급여에서 자동)">
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="text-sm">기간
            <input type="month" value={yyyymm} onChange={(e) => setYyyymm(e.target.value)}
              className="ml-2 rounded border border-slate-300 px-2 py-1" /></label>
          <label className="text-sm">재료비 풀
            <input type="number" value={material} onChange={(e) => setMaterial(e.target.value)}
              className="ml-2 w-32 rounded border border-slate-300 px-2 py-1" /></label>
          <label className="text-sm">간접비 풀
            <input type="number" value={overhead} onChange={(e) => setOverhead(e.target.value)}
              className="ml-2 w-32 rounded border border-slate-300 px-2 py-1" /></label>
        </div>
        <p className="mt-2 text-xs text-slate-500">
          동인: 인건비=인원수(자동) · 재료비=CASES · 간접비=AREA (동인값 미입력 시 인원수 폴백 — 원가팀 협의 후 정밀화)
        </p>
      </Card>

      <Card title="원가동인 값 입력 (정밀 배부 — 진료과별 CASES·AREA)">
        <div className="flex flex-wrap items-end gap-2">
          <input type="number" placeholder="진료과 ID" value={dv.department_id}
            onChange={(e) => setDv({ ...dv, department_id: e.target.value })}
            className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <select value={dv.driver} onChange={(e) => setDv({ ...dv, driver: e.target.value })}
            className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="CASES">CASES (건수)</option>
            <option value="AREA">AREA (면적)</option>
          </select>
          <input type="number" placeholder="동인값" value={dv.value}
            onChange={(e) => setDv({ ...dv, value: e.target.value })}
            className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          <Button onClick={() => saveDriver.mutate()} disabled={saveDriver.isPending || !dv.department_id}>저장</Button>
        </div>
      </Card>

      <Card title={`진료과별 ABC 배부 (${r.data?.yyyymm ?? yyyymm}) — 총 ${won(r.data?.grand_total ?? 0)}`}>
        <Table headers={["진료과", ...poolNames, "배부 원가"]}
          rows={(r.data?.objects ?? []).map((o) => [
            o.department ?? `#${o.object_id}`,
            ...poolNames.map((n) => won(o.pools[n] ?? 0)),
            won(o.total),
          ])} />
      </Card>
    </div>
  );
}
