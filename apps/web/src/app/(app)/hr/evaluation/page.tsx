"use client";
/** 인사 — 인사평가 (Tier3·0229): 차수·항목 편성·대상 배정·자기평가·평가자 채점·확정.
 *  자기평가는 전 직원(본인), 배정·확정은 관리자/부서장(서버 게이트·행 검증). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

const CYCLE_STATUS: Record<string, { label: string; tone: "gray" | "green" | "blue" }> = {
  DRAFT: { label: "준비", tone: "gray" }, OPEN: { label: "진행중", tone: "green" },
  CLOSED: { label: "마감", tone: "blue" },
};
const EVAL_STATUS: Record<string, { label: string; tone: "gray" | "amber" | "blue" | "green" }> = {
  PENDING: { label: "대기", tone: "gray" }, SELF_DONE: { label: "자기평가완료", tone: "amber" },
  EVAL_DONE: { label: "채점완료", tone: "blue" }, CONFIRMED: { label: "확정", tone: "green" },
};
const KIND: Record<string, string> = { COMPETENCY: "역량", PERFORMANCE: "실적", MBO: "MBO" };
const MANAGE = new Set(["DIRECTOR", "ADMIN", "MANAGER"]);

interface Cycle {
  id: number; period: string; title: string; cycle_type: string; status: string;
  start_on: string | null; end_on: string | null; evaluations: number;
}
interface EvalRow {
  id: number; employee_id: number; employee_no: string; name: string; department_id: number | null;
  evaluator_id: number | null; status: string;
  self_total: number | null; eval_total: number | null; final_grade: string | null;
}
interface Criterion {
  id: number; kind: string; title: string; weight: number; max_score: number;
  self_score: number | null; eval_score: number | null;
}
interface EvalDetail {
  id: number; employee_id: number; employee_name: string | null; status: string;
  self_total: number | null; eval_total: number | null; final_grade: string | null;
  self_comment: string | null; evaluator_comment: string | null; criteria: Criterion[];
}

export default function EvaluationPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const canManage = MANAGE.has(auth.claims()?.role ?? "");
  const [cycleId, setCycleId] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [evalId, setEvalId] = useState<number | null>(null);

  const cycles = useQuery({ queryKey: ["evcycles"], queryFn: () =>
    api<{ items: Cycle[] }>("/hr/eval/cycles") });
  const cur = cycles.data?.items.find((c) => c.id === cycleId) ?? cycles.data?.items[0];
  const evals = useQuery({ enabled: cur != null, queryKey: ["evals", cur?.id], queryFn: () =>
    api<{ items: EvalRow[] }>(`/hr/eval/cycles/${cur!.id}/evaluations`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["evcycles"] });
    qc.invalidateQueries({ queryKey: ["evals"] }); qc.invalidateQueries({ queryKey: ["evdetail"] }); };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">인사 — 인사평가</h1>
        {canManage && <Button onClick={() => setCreating(true)}>평가 차수 생성</Button>}
      </div>
      <p className="text-xs text-slate-500">
        평가 차수별 항목(역량·실적·MBO)을 편성하고 대상자에게 배정합니다. 자기평가는 본인이,
        채점은 지정 평가자/부서장이 수행하며 확정 시 가중 총점·등급이 산정됩니다.</p>

      <QueryErrorBanner queries={[cycles]} />
      <div className="flex flex-wrap gap-2">
        {(cycles.data?.items ?? []).map((c) => (
          <button key={c.id} onClick={() => setCycleId(c.id)}
            className={`rounded-md border px-3 py-1.5 text-sm ${
              (cur?.id === c.id) ? "border-blue-500 bg-blue-50" : "border-slate-200"}`}>
            {c.title} <Badge tone={CYCLE_STATUS[c.status]?.tone ?? "gray"}>
              {CYCLE_STATUS[c.status]?.label ?? c.status}</Badge>
          </button>))}
      </div>

      {cur && (
        <CyclePanel cycle={cur} evals={evals.data?.items ?? []} loading={evals.isFetching}
          canManage={canManage} onOpenEval={setEvalId} onChanged={refresh} onError={fail} />
      )}

      {creating && (
        <Modal title="평가 차수 생성" dirty onClose={() => setCreating(false)}>
          <CycleForm onDone={() => { setCreating(false); refresh(); }} onError={fail}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {evalId != null && (
        <EvalModal evalId={evalId} canManage={canManage} onClose={() => setEvalId(null)}
          onChanged={refresh} onError={fail} />
      )}
    </div>
  );
}

function CycleForm({ onDone, onError, onCancel }: {
  onDone: () => void; onError: (e: unknown) => void; onCancel: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ period: "", title: "", cycle_type: "ANNUAL" });
  const create = useMutation({
    mutationFn: () => api("/hr/eval/cycles", { method: "POST", body: JSON.stringify(f) }),
    onSuccess: () => { toast.ok("차수 생성 — 항목 편성 후 개시하세요"); onDone(); }, onError });
  return (
    <div className="space-y-2">
      <Input label="기간(예: 2026, 2026-H1)" value={f.period}
        onChange={(e) => setF({ ...f, period: e.target.value })} />
      <Input label="명칭" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
      <label className="block text-sm text-slate-600">유형
        <select value={f.cycle_type} onChange={(e) => setF({ ...f, cycle_type: e.target.value })}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="ANNUAL">연간</option><option value="HALF">반기</option>
          <option value="QUARTER">분기</option>
        </select>
      </label>
      <div className="flex gap-2">
        <Button disabled={create.isPending || !f.period.trim() || !f.title.trim()}
          onClick={() => create.mutate()}>생성</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function CyclePanel({ cycle, evals, loading, canManage, onOpenEval, onChanged, onError }: {
  cycle: Cycle; evals: EvalRow[]; loading: boolean; canManage: boolean;
  onOpenEval: (id: number) => void; onChanged: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [showCrit, setShowCrit] = useState(false);
  const criteria = useQuery({ queryKey: ["evcrit", cycle.id], queryFn: () =>
    api<{ items: Criterion[] }>(`/hr/eval/cycles/${cycle.id}/criteria`) });
  const act = useMutation({
    mutationFn: (path: string) => api(`/hr/eval/cycles/${cycle.id}/${path}`, { method: "POST" }),
    onSuccess: (_r, p) => { toast.ok(p === "open" ? "차수 개시" : "차수 마감"); onChanged(); },
    onError });
  const assignAll = useMutation({
    mutationFn: async () => {
      const emps = await api<{ items: { id: number }[] }>("/hr/employees?size=1000");
      return api(`/hr/eval/cycles/${cycle.id}/assign`, { method: "POST", body: JSON.stringify({
        assignments: emps.items.map((e) => ({ employee_id: e.id })) }) });
    },
    onSuccess: () => { toast.ok("재직자 일괄 배정"); onChanged(); }, onError });

  return (
    <Card title={`${cycle.title} (${cycle.period})`} action={canManage &&
      <span className="flex gap-1">
        <Button variant="secondary" size="sm" onClick={() => setShowCrit((v) => !v)}>
          항목 {criteria.data?.items.length ?? 0}</Button>
        {cycle.status === "DRAFT" && <Button size="sm" disabled={act.isPending}
          onClick={() => act.mutate("open")}>개시</Button>}
        {cycle.status === "OPEN" && <>
          <Button variant="secondary" size="sm" disabled={assignAll.isPending}
            onClick={() => assignAll.mutate()}>재직자 배정</Button>
          <Button size="sm" disabled={act.isPending} onClick={() => act.mutate("close")}>마감</Button></>}
      </span>}>
      {showCrit && (
        <CriteriaEditor cycle={cycle} canManage={canManage}
          items={criteria.data?.items ?? []} onError={onError}
          onChanged={() => criteria.refetch()} />
      )}
      <Table headers={["사번", "이름", "상태", "자기점수", "평가점수", "등급", ""]}
        rows={evals.map((e) => [
          e.employee_no, e.name,
          <Badge key="s" tone={EVAL_STATUS[e.status]?.tone ?? "gray"}>
            {EVAL_STATUS[e.status]?.label ?? e.status}</Badge>,
          e.self_total != null ? e.self_total.toFixed(1) : "—",
          e.eval_total != null ? e.eval_total.toFixed(1) : "—",
          e.final_grade ?? "—",
          <Button key="a" variant="secondary" size="sm" onClick={() => onOpenEval(e.id)}>열기</Button>,
        ])} empty={loading ? "불러오는 중…" : "배정된 평가 없음"} />
    </Card>
  );
}

function CriteriaEditor({ cycle, items, canManage, onChanged, onError }: {
  cycle: Cycle; items: Criterion[]; canManage: boolean;
  onChanged: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ title: "", kind: "COMPETENCY", weight: "1", max_score: "100" });
  const add = useMutation({
    mutationFn: () => api(`/hr/eval/cycles/${cycle.id}/criteria`, { method: "POST",
      body: JSON.stringify({ title: f.title, kind: f.kind, weight: Number(f.weight),
        max_score: Number(f.max_score) }) }),
    onSuccess: () => { toast.ok("항목 추가"); setF({ ...f, title: "" }); onChanged(); }, onError });
  return (
    <div className="mb-3 rounded-md border border-slate-200 p-2">
      <Table headers={["구분", "항목", "가중치", "만점"]}
        rows={items.map((c) => [KIND[c.kind] ?? c.kind, c.title, c.weight, c.max_score])}
        empty="항목 없음" />
      {canManage && cycle.status === "DRAFT" && (
        <div className="mt-2 flex flex-wrap items-end gap-2">
          <label className="text-sm text-slate-600">구분
            <select value={f.kind} onChange={(e) => setF({ ...f, kind: e.target.value })}
              className="block rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              {Object.entries(KIND).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
            </select>
          </label>
          <Input label="항목명" value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} />
          <Input label="가중치" value={f.weight} inputMode="decimal" className="w-20"
            onChange={(e) => setF({ ...f, weight: e.target.value.replace(/[^0-9.]/g, "") })} />
          <Input label="만점" value={f.max_score} inputMode="numeric" className="w-20"
            onChange={(e) => setF({ ...f, max_score: e.target.value.replace(/[^0-9]/g, "") })} />
          <Button size="sm" disabled={add.isPending || !f.title.trim()}
            onClick={() => add.mutate()}>항목 추가</Button>
        </div>
      )}
      {cycle.status !== "DRAFT" && <p className="mt-1 text-xs text-slate-400">항목은 준비(DRAFT) 상태에서만 편성합니다.</p>}
    </div>
  );
}

function EvalModal({ evalId, canManage, onClose, onChanged, onError }: {
  evalId: number; canManage: boolean; onClose: () => void;
  onChanged: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [selfScores, setSelfScores] = useState<Record<number, string>>({});
  const [evalScores, setEvalScores] = useState<Record<number, string>>({});
  const detail = useQuery({ queryKey: ["evdetail", evalId], queryFn: () =>
    api<EvalDetail>(`/hr/eval/evaluations/${evalId}`) });
  const d = detail.data;
  // 서술 근거 — API가 comment를 받고 응답에도 있는데 표시·입력 경로가 전무했다.
  // 점수만 있고 근거가 없으면 확정·이의 대응이 불가능하다.
  const [selfComment, setSelfComment] = useState("");
  const [evalComment, setEvalComment] = useState("");

  const submitSelf = useMutation({
    mutationFn: () => api(`/hr/eval/evaluations/${evalId}/self`, { method: "POST",
      body: JSON.stringify({ scores: (d?.criteria ?? [])
        .filter((c) => selfScores[c.id] !== undefined && selfScores[c.id] !== "")
        .map((c) => ({ criterion_id: c.id, score: Number(selfScores[c.id]) })),
        comment: selfComment || null }) }),
    onSuccess: () => { toast.ok("자기평가 제출"); detail.refetch(); onChanged(); }, onError });
  const submitEval = useMutation({
    mutationFn: () => api(`/hr/eval/evaluations/${evalId}/score`, { method: "POST",
      body: JSON.stringify({ scores: (d?.criteria ?? [])
        .filter((c) => evalScores[c.id] !== undefined && evalScores[c.id] !== "")
        .map((c) => ({ criterion_id: c.id, score: Number(evalScores[c.id]) })),
        comment: evalComment || null }) }),
    onSuccess: () => { toast.ok("채점 제출"); detail.refetch(); onChanged(); }, onError });
  const confirm = useMutation({
    mutationFn: () => api(`/hr/eval/evaluations/${evalId}/confirm`, { method: "POST",
      body: JSON.stringify({}) }),
    onSuccess: () => { toast.ok("평가 확정"); detail.refetch(); onChanged(); }, onError });

  return (
    <Modal title={d ? `${d.employee_name ?? ""} 평가` : "평가"} size="lg" onClose={onClose}>
      {!d ? <p className="text-sm text-slate-400">불러오는 중…</p> : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <Badge tone={EVAL_STATUS[d.status]?.tone ?? "gray"}>{EVAL_STATUS[d.status]?.label ?? d.status}</Badge>
            {d.self_total != null && <span>자기 <b>{d.self_total.toFixed(1)}</b></span>}
            {d.eval_total != null && <span>평가 <b>{d.eval_total.toFixed(1)}</b></span>}
            {d.final_grade && <Badge tone="green">등급 {d.final_grade}</Badge>}
          </div>
          <Table headers={["구분", "항목", "가중치", "만점", "자기", "평가자"]}
            rows={d.criteria.map((c) => [
              KIND[c.kind] ?? c.kind, c.title, c.weight, c.max_score,
              d.status === "PENDING" || d.status === "SELF_DONE"
                ? <Input key="s" inputMode="decimal" className="w-16"
                    value={selfScores[c.id] ?? (c.self_score != null ? String(c.self_score) : "")}
                    onChange={(e) => setSelfScores({ ...selfScores,
                      [c.id]: e.target.value.replace(/[^0-9.]/g, "") })} />
                : (c.self_score != null ? c.self_score : "—"),
              canManage && (d.status === "SELF_DONE" || d.status === "EVAL_DONE")
                ? <Input key="e" inputMode="decimal" className="w-16"
                    value={evalScores[c.id] ?? (c.eval_score != null ? String(c.eval_score) : "")}
                    onChange={(e) => setEvalScores({ ...evalScores,
                      [c.id]: e.target.value.replace(/[^0-9.]/g, "") })} />
                : (c.eval_score != null ? c.eval_score : "—"),
            ])} empty="항목 없음" />
          <div className="space-y-2 border-t border-slate-100 pt-2">
            <div>
              <p className="mb-1 text-xs font-semibold text-slate-600">자기 의견</p>
              {(d.status === "PENDING" || d.status === "SELF_DONE")
                ? <textarea rows={2} value={selfComment || d.self_comment || ""}
                    onChange={(e) => setSelfComment(e.target.value)}
                    placeholder="자기평가 근거를 적어주세요"
                    className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm" />
                : <p className="text-sm text-slate-700">{d.self_comment ?? "—"}</p>}
            </div>
            <div>
              <p className="mb-1 text-xs font-semibold text-slate-600">평가자 의견</p>
              {canManage && (d.status === "SELF_DONE" || d.status === "EVAL_DONE")
                ? <textarea rows={2} value={evalComment || d.evaluator_comment || ""}
                    onChange={(e) => setEvalComment(e.target.value)}
                    placeholder="채점 근거를 적어주세요"
                    className="w-full rounded-md border border-slate-300 px-2 py-1 text-sm" />
                : <p className="text-sm text-slate-700">{d.evaluator_comment ?? "—"}</p>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2 border-t border-slate-100 pt-2">
            {(d.status === "PENDING" || d.status === "SELF_DONE") &&
              <Button size="sm" disabled={submitSelf.isPending}
                onClick={() => submitSelf.mutate()}>자기평가 제출</Button>}
            {canManage && (d.status === "SELF_DONE" || d.status === "EVAL_DONE") &&
              <Button size="sm" disabled={submitEval.isPending}
                onClick={() => submitEval.mutate()}>채점 제출</Button>}
            {canManage && d.status === "EVAL_DONE" &&
              <Button variant="success" size="sm" disabled={confirm.isPending}
                onClick={() => confirm.mutate()}>확정</Button>}
          </div>
        </div>
      )}
    </Modal>
  );
}
