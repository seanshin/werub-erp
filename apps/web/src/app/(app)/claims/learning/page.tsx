"use client";
/** 의료보험 — 사전심사 학습 사례(심사 학습 데이터). 결정된 적발(인정=삭감위험/오탐=정상)을
 *  자동 수확 + 수동 등록 → 품질 승인·의료진 확인분만 학습 적격 → JSONL 내보내기. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface LCase {
  id: number; source_finding_id: number | null; chart_no: string | null;
  care_month: string | null; rule_code: string | null; rule_type: string | null;
  code: string | null; amount: number; label: string; rationale: string | null;
  physician_confirmed: boolean; tags: string | null; quality: string; source: string;
  eligible: boolean;
}
interface ListResp {
  items: LCase[];
  summary: { total: number; eligible: number; reduction: number; false_positive: number };
}

const LABEL_TONE: Record<string, "amber" | "gray"> = { REDUCTION: "amber", FALSE_POSITIVE: "gray" };
const LABEL_KO: Record<string, string> = { REDUCTION: "삭감위험", FALSE_POSITIVE: "오탐" };
const QUALITY_TONE: Record<string, "gray" | "green" | "red"> = {
  DRAFT: "gray", APPROVED: "green", REJECTED: "red" };

export default function LearningCasesPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [label, setLabel] = useState("");
  const [quality, setQuality] = useState("");
  const [eligibleOnly, setEligibleOnly] = useState(false);
  const [add, setAdd] = useState<{ label: string; rationale: string; source_finding_id: string }>(
    { label: "FALSE_POSITIVE", rationale: "", source_finding_id: "" });
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const q = new URLSearchParams();
  if (label) q.set("label", label);
  if (quality) q.set("quality", quality);
  if (eligibleOnly) q.set("eligible_only", "true");
  const list = useQuery({
    queryKey: ["learning-cases", label, quality, eligibleOnly],
    queryFn: () => api<ListResp>(`/claims/pre-review/learning-cases?${q.toString()}`),
  });
  const refresh = () => qc.invalidateQueries({ queryKey: ["learning-cases"] });

  const harvest = useMutation({
    mutationFn: () => api<{ added: number; scanned: number }>(
      "/claims/pre-review/learning-cases/harvest", { method: "POST" }),
    onSuccess: (r) => { toast.ok(`자동 수확 완료 — 신규 ${r.added}건 (스캔 ${r.scanned})`); refresh(); },
    onError: fail,
  });
  const patch = useMutation({
    mutationFn: (p: { id: number; quality?: string; active?: boolean }) =>
      api(`/claims/pre-review/learning-cases/${p.id}`, { method: "PATCH",
        body: JSON.stringify({ quality: p.quality, active: p.active }) }),
    onSuccess: () => { toast.ok("반영됨"); refresh(); }, onError: fail,
  });
  const create = useMutation({
    mutationFn: () => api("/claims/pre-review/learning-cases", { method: "POST",
      body: JSON.stringify({ label: add.label, rationale: add.rationale || undefined,
        source_finding_id: add.source_finding_id ? Number(add.source_finding_id) : undefined }) }),
    onSuccess: () => { toast.ok("수동 등록 완료"); setAdd({ label: "FALSE_POSITIVE", rationale: "", source_finding_id: "" }); refresh(); },
    onError: fail,
  });

  const s = list.data?.summary;
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">사전심사 학습 사례 (심사 학습 데이터)</h1>
        <div className="flex gap-2">
          <Button onClick={() => harvest.mutate()} disabled={harvest.isPending}>
            {harvest.isPending ? "수확 중…" : "결정 적발 자동 수확"}</Button>
          <Button variant="secondary"
            onClick={() => apiDownload("/claims/pre-review/learning-cases/export",
              "prereview-learning.jsonl").catch(fail)}>학습셋 내보내기 (JSONL)</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        결정된 적발(인정=삭감위험 · 오탐=정상)을 라벨된 학습 데이터로 등록·관리합니다. 의료진 확인분은
        즉시 승인(고품질)되며, <b>품질 승인 + (의학판단 룰은 의료진 확인) 충족분만 학습 적격</b>으로
        내보냅니다. 자동오탐(P2)은 순환 학습 방지를 위해 수확에서 제외됩니다.</p>
      {list.isError && <p className="text-sm text-red-600">조회 권한이 없습니다 (MANAGER↑)</p>}

      {s && (
        <div className="flex flex-wrap gap-x-6 gap-y-1 rounded-md bg-slate-50 px-3 py-2 text-sm">
          <span>총 <b>{s.total}</b></span>
          <span className="text-green-700">학습 적격 <b>{s.eligible}</b></span>
          <span className="text-amber-700">삭감위험 {s.reduction}</span>
          <span className="text-slate-500">오탐 {s.false_positive}</span>
        </div>
      )}

      <div className="flex flex-wrap items-end gap-2">
        <label className="text-xs text-slate-500">라벨
          <select value={label} onChange={(e) => setLabel(e.target.value)}
            className="ml-1 rounded border border-slate-300 px-2 py-1 text-sm">
            <option value="">전체</option><option value="REDUCTION">삭감위험</option>
            <option value="FALSE_POSITIVE">오탐</option></select></label>
        <label className="text-xs text-slate-500">품질
          <select value={quality} onChange={(e) => setQuality(e.target.value)}
            className="ml-1 rounded border border-slate-300 px-2 py-1 text-sm">
            <option value="">전체</option><option value="DRAFT">초안</option>
            <option value="APPROVED">승인</option><option value="REJECTED">반려</option></select></label>
        <label className="flex items-center gap-1 text-xs text-slate-500">
          <input type="checkbox" checked={eligibleOnly}
            onChange={(e) => setEligibleOnly(e.target.checked)} /> 학습 적격만</label>
      </div>

      <Card title={`학습 사례 (${list.data?.items.length ?? 0})`}>
        <Table headers={["라벨", "룰", "코드", "금액", "근거", "의료진", "품질", "소스", "적격", ""]}
          rows={(list.data?.items ?? []).map((c) => [
            <Badge key="l" tone={LABEL_TONE[c.label]}>{LABEL_KO[c.label] ?? c.label}</Badge>,
            <span key="r" className="text-xs">{c.rule_type ?? c.rule_code ?? "—"}</span>,
            <code key="c" className="text-xs">{c.code ?? "—"}</code>,
            c.amount ? c.amount.toLocaleString() : "—",
            <span key="ra" className="text-xs text-slate-500" title={c.rationale ?? ""}>
              {(c.rationale ?? "—").slice(0, 24)}</span>,
            c.physician_confirmed ? <Badge key="p" tone="green">확인</Badge> : "—",
            <Badge key="q" tone={QUALITY_TONE[c.quality]}>{c.quality}</Badge>,
            <span key="s" className="text-[11px] text-slate-400">{c.source}</span>,
            c.eligible ? <Badge key="e" tone="green">✓ 적격</Badge> : "",
            <span key="x" className="flex gap-1">
              {c.quality !== "APPROVED" && (
                <Button variant="success" size="sm"
                  onClick={() => patch.mutate({ id: c.id, quality: "APPROVED" })}>승인</Button>)}
              {c.quality !== "REJECTED" && (
                <Button variant="neutral" size="sm"
                  onClick={() => patch.mutate({ id: c.id, quality: "REJECTED" })}>반려</Button>)}
            </span>,
          ])} empty="학습 사례 없음 — 자동 수확을 실행하세요" />
      </Card>

      <Card title="수동 등록">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-500">라벨
            <select value={add.label} onChange={(e) => setAdd({ ...add, label: e.target.value })}
              className="ml-1 rounded border border-slate-300 px-2 py-1 text-sm">
              <option value="FALSE_POSITIVE">오탐</option>
              <option value="REDUCTION">삭감위험</option></select></label>
          <Input label="적발 ID(선택)" value={add.source_finding_id}
            onChange={(e) => setAdd({ ...add, source_finding_id: e.target.value.replace(/[^0-9]/g, "") })} />
          <div className="flex-1 min-w-[240px]">
            <Input label="근거/사유" value={add.rationale}
              onChange={(e) => setAdd({ ...add, rationale: e.target.value })}
              placeholder="이 사례를 학습 데이터로 쓰는 근거" /></div>
          <Button onClick={() => create.mutate()} disabled={create.isPending}>등록</Button>
        </div>
      </Card>
    </div>
  );
}
