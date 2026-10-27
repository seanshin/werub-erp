"use client";
/** 규제 — 환자안전사고 보고(환자안전법 §14 자율보고). 전 직원 보고(익명 가능)·검토/조치/종결(승인권).
 *  임상 RCA는 HIS/임상 소관, ERP는 보고관리·분류·통계·재발방지 추적. 인증평가 필수 지표. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { todayLocal } from "@/lib/dates";
import { dt } from "@/lib/format";
import { useToast } from "@/components/toast";

const MANAGE = new Set(["DIRECTOR", "ADMIN", "MANAGER"]);
const TYPE_KR: Record<string, string> = { NEAR_MISS: "근접오류", ADVERSE: "위해사건", SENTINEL: "적신호사건" };
const SEV_KR: Record<string, string> = { NONE: "없음", MILD: "경미", MODERATE: "중등도", SEVERE: "중대", DEATH: "사망" };
const STATUS_KR: Record<string, string> = { REPORTED: "접수", REVIEWING: "검토중", ACTIONED: "조치완료", CLOSED: "종결" };
const sevTone = (s: string) => ["DEATH", "SEVERE"].includes(s) ? "red" : s === "MODERATE" ? "amber" : "gray";
const stTone = (s: string) => s === "CLOSED" ? "green" : s === "ACTIONED" ? "blue" : s === "REVIEWING" ? "amber" : "gray";
// 상태 전이: 접수→검토중→조치완료→종결
const NEXT: Record<string, { to: string; label: string }[]> = {
  REPORTED: [{ to: "REVIEWING", label: "검토 시작" }],
  REVIEWING: [{ to: "ACTIONED", label: "조치 완료" }],
  ACTIONED: [{ to: "CLOSED", label: "종결" }],
  CLOSED: [],
};

interface Incident {
  id: number; occurred_on: string; incident_type: string; severity: string;
  category: string | null; status: string;
  // 경위·조치가 없으면 검토/종결을 근거 없이 누르게 된다(환자안전법 §14 검토 기록)
  // 경위·조치는 검토 권한자에게만 내려온다(비권한자는 null·narrative_visible=false)
  description: string | null; action: string | null; narrative_visible: boolean;
  reviewed_by_name: string | null; reviewed_at: string | null; anonymous: boolean;
}
interface Stats {
  since: string; until: string; total: number; open: number;
  by_type: Record<string, number>; by_severity: Record<string, number>;
}

export default function SafetyPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const canManage = MANAGE.has(auth.claims()?.role ?? "");
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [reporting, setReporting] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const monthAgo = (() => { const d = new Date(); d.setMonth(d.getMonth() - 3);
    return d.toISOString().slice(0, 10); })();

  const incidents = useQuery({ queryKey: ["safety-incidents", statusFilter], queryFn: () =>
    api<{ items: Incident[] }>(`/regulatory/safety/incidents${statusFilter ? `?status=${statusFilter}` : ""}`) });
  const stats = useQuery({ queryKey: ["safety-stats"], queryFn: () =>
    api<Stats>(`/regulatory/safety/stats?start=${monthAgo}&end=${todayLocal()}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["safety-incidents"] });
    qc.invalidateQueries({ queryKey: ["safety-stats"] }); };

  const transition = useMutation({
    mutationFn: (v: { id: number; to: string }) =>
      api(`/regulatory/safety/incidents/${v.id}`, { method: "PATCH",
        body: JSON.stringify({ to_status: v.to }) }),
    onSuccess: () => { toast.ok("상태 변경"); refresh(); }, onError: fail });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">규제 — 환자안전사고 보고</h1>
        <Button onClick={() => setReporting(true)}>사고 보고</Button>
      </div>
      <p className="text-xs text-slate-500">
        환자안전법 §14 자율보고 — 근접오류·위해사건·적신호사건을 접수·검토·종결 관리합니다.
        보고는 전 직원(익명 가능), 보고자는 보호됩니다. 검토·조치·종결은 승인 권한.</p>

      <QueryErrorBanner queries={[incidents, stats]} />

      {stats.data && (
        <Card title={`최근 3개월 통계 (${stats.data.since} ~ ${stats.data.until})`}>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="rounded-lg border border-slate-200 p-2">
              <p className="text-xs text-slate-500">전체 보고</p>
              <p className="text-sm font-bold">{stats.data.total}건</p></div>
            <div className="rounded-lg border border-amber-200 bg-amber-50 p-2">
              <p className="text-xs text-slate-500">미종결</p>
              <p className="text-sm font-bold text-amber-700">{stats.data.open}건</p></div>
            <div className="rounded-lg border border-slate-200 p-2">
              <p className="text-xs text-slate-500">유형별</p>
              <p className="text-xs">{Object.entries(stats.data.by_type).map(([k, v]) =>
                `${TYPE_KR[k] ?? k} ${v}`).join(" · ") || "—"}</p></div>
            <div className="rounded-lg border border-slate-200 p-2">
              <p className="text-xs text-slate-500">중대도별</p>
              <p className="text-xs">{Object.entries(stats.data.by_severity).map(([k, v]) =>
                `${SEV_KR[k] ?? k} ${v}`).join(" · ") || "—"}</p></div>
          </div>
        </Card>
      )}

      <Card title="사고 보고 목록" action={
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체 상태</option>
          {Object.entries(STATUS_KR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </select>}>
        <Table headers={["발생일", "유형", "중대도", "분류",
                         ...(canManage ? ["경위 · 조치", "상태", "검토자"] : ["상태"]),
                         canManage ? "처리" : ""]}
          rows={(incidents.data?.items ?? []).map((r) => [
            r.occurred_on,
            TYPE_KR[r.incident_type] ?? r.incident_type,
            <Badge key="s" tone={sevTone(r.severity)}>{SEV_KR[r.severity] ?? r.severity}</Badge>,
            r.category ?? "—",
            ...(canManage ? [
              <div key="d" className="max-w-md space-y-0.5">
                <p className="whitespace-pre-wrap text-xs text-slate-700">{r.description}</p>
                {r.action
                  ? <p className="whitespace-pre-wrap text-xs text-emerald-700">
                      <span className="font-semibold">조치</span> · {r.action}</p>
                  : r.status === "CLOSED" &&
                    <p className="text-xs text-amber-600">재발방지 조치 미기재</p>}
              </div>,
              <Badge key="st" tone={stTone(r.status)}>{STATUS_KR[r.status] ?? r.status}</Badge>,
              <span key="rv" className="whitespace-nowrap text-xs text-slate-500">
                {r.reviewed_by_name ?? "—"}
                {r.reviewed_at && <><br />{dt(r.reviewed_at).slice(0, 10)}</>}
              </span>,
            ] : [
              <Badge key="st" tone={stTone(r.status)}>{STATUS_KR[r.status] ?? r.status}</Badge>,
            ]),
            canManage
              ? <span key="a" className="flex gap-1">
                  {(NEXT[r.status] ?? []).map((n) => (
                    <Button key={n.to} size="sm" variant="secondary" disabled={transition.isPending}
                      onClick={() => transition.mutate({ id: r.id, to: n.to })}>{n.label}</Button>))}
                  {(NEXT[r.status] ?? []).length === 0 && <span className="text-xs text-slate-400">—</span>}
                </span>
              : "",
          ])} empty={incidents.isFetching ? "불러오는 중…" : "보고 없음"} />
      </Card>

      {reporting && <ReportModal onDone={() => { setReporting(false); refresh(); }}
        onError={fail} onClose={() => setReporting(false)} />}
    </div>
  );
}

function ReportModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ occurred_on: todayLocal(), incident_type: "NEAR_MISS",
    severity: "NONE", category: "", description: "", anonymous: false });
  const report = useMutation({
    mutationFn: () => api("/regulatory/safety/incidents", { method: "POST", body: JSON.stringify({
      occurred_on: f.occurred_on, incident_type: f.incident_type, severity: f.severity,
      category: f.category || null, description: f.description, anonymous: f.anonymous }) }),
    onSuccess: () => { toast.ok("보고 접수"); onDone(); }, onError });
  return (
    <Modal title="환자안전사고 보고" dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="발생일" type="date" value={f.occurred_on}
          onChange={(e) => setF({ ...f, occurred_on: e.target.value })} />
        <label className="block text-sm text-slate-600">유형
          <select value={f.incident_type} onChange={(e) => setF({ ...f, incident_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(TYPE_KR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <label className="block text-sm text-slate-600">위해 정도
          <select value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(SEV_KR).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="분류(선택·예: 낙상·투약오류)" value={f.category}
          onChange={(e) => setF({ ...f, category: e.target.value })} />
        <label className="block text-sm text-slate-600">경위
          <textarea value={f.description} rows={3}
            onChange={(e) => setF({ ...f, description: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
        </label>
        <label className="flex items-center gap-2 text-sm text-slate-600">
          <input type="checkbox" checked={f.anonymous}
            onChange={(e) => setF({ ...f, anonymous: e.target.checked })} />
          익명 보고 (보고자 미기록)
        </label>
        <Button disabled={report.isPending || !f.description.trim()} onClick={() => report.mutate()}>
          보고 접수</Button>
      </div>
    </Modal>
  );
}
