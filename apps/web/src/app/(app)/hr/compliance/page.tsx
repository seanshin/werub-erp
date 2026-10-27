"use client";
/** 인사 — 법정의무교육·건강검진 추적 (P3): 재직자 × 항목 매트릭스, 인증평가 대비. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { HrEmployeePicker } from "@/components/employee-picker";

interface Cell { state: string; done_on?: string; valid_until?: string }
interface Status {
  as_of: string;
  categories: { key: string; kind: string; label: string }[];
  items: { employee_id: number; employee_no: string; name: string;
    job_category: string | null; cells: Record<string, Cell> }[];
  summary: { category: string; label: string; required: number;
    compliant: number; rate: number | null }[];
}

const TONE: Record<string, { label: string; tone: "green" | "amber" | "red" | "gray" }> = {
  OK: { label: "이수", tone: "green" }, DUE_SOON: { label: "임박", tone: "amber" },
  OVERDUE: { label: "만료", tone: "red" }, NONE: { label: "미이수", tone: "gray" },
};

export default function CompliancePage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [form, setForm] = useState({ employee_id: "", category: "PRIVACY", done_on: "" });

  const status = useQuery({ queryKey: ["compliance"], queryFn: () =>
    api<Status>("/hr/compliance/status") });

  const record = useMutation({
    mutationFn: () => api("/hr/compliance", { method: "POST", body: JSON.stringify({
      employee_id: Number(form.employee_id), category: form.category,
      done_on: form.done_on }) }),
    onSuccess: () => { toast.ok("이수 기록 완료");
      qc.invalidateQueries({ queryKey: ["compliance"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "기록 실패"),
  });

  const d = status.data;
  const input = "rounded-md border border-slate-300 px-2 py-1 text-sm";
  const overdue = d?.items.reduce((n, i) =>
    n + Object.values(i.cells).filter((c) => c.state === "OVERDUE").length, 0) ?? 0;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 법정의무교육·건강검진</h1>
        <Button variant="neutral" onClick={() => apiDownload("/hr/compliance/status.xlsx", "compliance-status.xlsx")}>현황 Excel (인증평가 제출용)</Button>
      </div>
      {overdue > 0 && (
        <p className="rounded-md bg-rose-50 px-3 py-1.5 text-sm text-rose-700">
          만료 {overdue}건 — 재이수/재검진이 필요합니다</p>
      )}

      <Card title="항목별 이수율">
        <Table headers={["항목", "대상", "이수", "이수율"]}
          rows={(d?.summary ?? []).map((s) => [
            s.label, `${s.required}명`, `${s.compliant}명`,
            s.rate === null ? "-" :
              <span key="r" className={s.rate < 0.8 ? "font-semibold text-rose-600" : ""}>
                {(s.rate * 100).toFixed(0)}%</span>])} />
      </Card>

      <Card title="이수 기록" action={
        <span className="text-[11px] text-slate-400">주기(연 1회) 만료 자동 산정 · 감사기록</span>}>
        <div className="flex flex-wrap items-end gap-2">
          <HrEmployeePicker value={form.employee_id}
            onPick={(id) => setForm({ ...form, employee_id: id })} />
          <select value={form.category} className={input}
            onChange={(e) => setForm({ ...form, category: e.target.value })}>
            {(d?.categories ?? []).map((c) => <option key={c.key} value={c.key}>{c.label}</option>)}
          </select>
          <input type="date" value={form.done_on} className={input}
            onChange={(e) => setForm({ ...form, done_on: e.target.value })} />
          <Button disabled={!form.employee_id || !form.done_on || record.isPending}
            onClick={() => record.mutate()}>기록</Button>
        </div>
      </Card>

      <Card title={`재직자 현황 매트릭스 (기준 ${d?.as_of ?? ""})`}>
        <Table headers={["사번", "이름", ...(d?.categories ?? []).map((c) => c.label)]}
          rows={(d?.items ?? []).map((i) => [
            i.employee_no, i.name,
            ...(d?.categories ?? []).map((c) => {
              const cell = i.cells[c.key];
              if (!cell || cell.state === "N/A") {
                return <span key={c.key} className="text-xs text-slate-300">—</span>;
              }
              const t = TONE[cell.state];
              return <span key={c.key} title={cell.valid_until ? `만료 ${cell.valid_until}` : ""}>
                <Badge tone={t.tone}>{t.label}</Badge></span>;
            }),
          ])} />
      </Card>
    </div>
  );
}
