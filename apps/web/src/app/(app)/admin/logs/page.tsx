"use client";
/** 감사·접속기록 조회 (원장·관리자 한정 — NFR Audit). 행위자·동작·대상·대상ID·경로·기간 필터. */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { dt } from "@/lib/format";
import { Badge, Card, Table } from "@/components/ui";

interface AuditRow { id: number; actor: number | null; actor_name: string | null;
                     action: string; entity: string; target_id: string | null; at: string;
                     // 서버가 계좌·급여·부양가족·주민번호를 가린 스냅샷(masked=true)
                     before: Record<string, unknown> | null;
                     after: Record<string, unknown> | null }
interface AccessRow { id: number; actor: number; action: string; entity: string;
                      target_id: string | null; route: string | null; at: string }

const AUDIT_ACTIONS = ["CREATE", "UPDATE", "DELETE", "APPROVE", "REJECT", "REVERSE",
  "REPROCESS", "ATTACH_UPLOAD", "ATTACH_DELETE", "WS_PASS_CREATE", "WS_PASS_REVOKE", "LOGIN"];
const ACCESS_ACTIONS = ["VIEW", "EXPORT", "PRINT"];
const SIZE = 50;
const inp = "rounded-md border border-slate-300 px-2 py-1.5 text-sm";

export default function LogsPage() {
  const [tab, setTab] = useState<"audit" | "access">("audit");
  const [f, setF] = useState({ actor: "", action: "", entity: "", target_id: "",
    route: "", date_from: "", date_to: "" });
  const [page, setPage] = useState(1);
  const set = (k: keyof typeof f) => (v: string) => { setF((s) => ({ ...s, [k]: v })); setPage(1); };
  const reset = () => { setF({ actor: "", action: "", entity: "", target_id: "", route: "",
    date_from: "", date_to: "" }); setPage(1); };
  const hasFilter = Object.values(f).some(Boolean);

  const qs = () => {
    const p = new URLSearchParams({ size: String(SIZE), page: String(page) });
    if (f.actor) p.set("actor", f.actor);
    if (f.action) p.set("action", f.action);
    if (f.entity) p.set("entity", f.entity);
    if (f.target_id) p.set("target_id", f.target_id);
    if (f.date_from) p.set("date_from", f.date_from);
    if (f.date_to) p.set("date_to", f.date_to);
    if (tab === "access" && f.route) p.set("route", f.route);
    return p.toString();
  };

  const ep = tab === "audit" ? "audit-logs" : "access-logs";
  const logs = useQuery({ queryKey: [ep, f, page],
    queryFn: () => api<{ items: (AuditRow & AccessRow)[]; count: number }>(
      `/admin/${ep}?${qs()}`) });
  const total = logs.data?.count ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / SIZE));
  const actions = tab === "audit" ? AUDIT_ACTIONS : ACCESS_ACTIONS;

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">감사·접속기록</h1>

      <div className="flex gap-2">
        {([["audit", "변경 감사 (append-only)"], ["access", "접속기록 (조회·반출)"]] as const)
          .map(([k, label]) => (
          <button key={k} onClick={() => { setTab(k); setPage(1); }}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${tab === k
              ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600 hover:bg-slate-200"}`}>
            {label}</button>
        ))}
      </div>

      <Card title="필터">
        <div className="flex flex-wrap items-end gap-2">
          <label className="text-xs text-slate-500">행위자 ID
            <input placeholder="예: 10" value={f.actor} inputMode="numeric"
              onChange={(e) => set("actor")(e.target.value)} className={`${inp} mt-0.5 block w-28`} /></label>
          <label className="text-xs text-slate-500">동작
            <select value={f.action} onChange={(e) => set("action")(e.target.value)}
              className={`${inp} mt-0.5 block`}>
              <option value="">전체</option>
              {actions.map((a) => <option key={a} value={a}>{a}</option>)}
            </select></label>
          <label className="text-xs text-slate-500">대상(엔티티)
            <input placeholder="예: finance.vouchers" value={f.entity}
              onChange={(e) => set("entity")(e.target.value)} className={`${inp} mt-0.5 block w-52`} /></label>
          <label className="text-xs text-slate-500">대상 ID
            <input placeholder="target_id" value={f.target_id}
              onChange={(e) => set("target_id")(e.target.value)} className={`${inp} mt-0.5 block w-32`} /></label>
          {tab === "access" && (
            <label className="text-xs text-slate-500">경로
              <input placeholder="예: /export" value={f.route}
                onChange={(e) => set("route")(e.target.value)} className={`${inp} mt-0.5 block w-40`} /></label>)}
          <label className="text-xs text-slate-500">시작일
            <input type="date" value={f.date_from}
              onChange={(e) => set("date_from")(e.target.value)} className={`${inp} mt-0.5 block`} /></label>
          <label className="text-xs text-slate-500">종료일
            <input type="date" value={f.date_to}
              onChange={(e) => set("date_to")(e.target.value)} className={`${inp} mt-0.5 block`} /></label>
          {hasFilter && (
            <button onClick={reset}
              className="rounded-md px-2 py-1.5 text-sm text-slate-500 hover:text-slate-900">초기화</button>)}
          <span className="ml-auto self-center text-xs text-slate-400">{total.toLocaleString()}건</span>
        </div>
      </Card>

      {logs.isError && (
        <p className="text-sm text-red-600">로그를 불러오지 못했습니다 (원장·관리자 권한 필요)</p>)}

      <Card title={tab === "audit"
        ? "변경 감사 (audit_log — append-only)"
        : "접속기록 (access_log — 조회·반출, 법정 2년 보존)"}>
        <Table headers={tab === "audit"
          ? ["#", "행위자", "동작", "대상", "ID", "변경 내역", "일시"]
          : ["#", "행위자", "동작", "대상", "경로", "일시"]}
          empty={logs.isFetching ? "불러오는 중…" : "해당 조건의 기록 없음"}
          rows={(logs.data?.items ?? []).map((r) => tab === "audit" ? [
            r.id,
            <span key="ac" className="whitespace-nowrap">
              {r.actor_name ?? (r.actor != null ? `#${r.actor}` : "-")}</span>,
            <Badge key="a" tone={r.action === "DELETE" ? "red" : "gray"}>{r.action}</Badge>,
            r.entity, r.target_id ?? "-",
            <Diff key="d" before={r.before} after={r.after} />,
            dt(r.at),
          ] : [
            r.id, r.actor,
            <Badge key="a" tone={r.action === "EXPORT" ? "amber" : "gray"}>{r.action}</Badge>,
            r.entity, r.route ?? "-", dt(r.at),
          ])} />
        {tab === "audit" && (
          <p className="mt-2 text-xs text-slate-400">
            변경 내역의 계좌번호·급여·부양가족·주민등록번호는 서버에서 가려 보냅니다(***).
          </p>)}
        {total > SIZE && (
          <div className="mt-3 flex items-center justify-center gap-3 text-sm">
            <button onClick={() => setPage((p) => Math.max(1, p - 1))} disabled={page <= 1}
              className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">이전</button>
            <span className="text-slate-500">{page} / {lastPage}</span>
            <button onClick={() => setPage((p) => Math.min(lastPage, p + 1))} disabled={page >= lastPage}
              className="rounded-md border border-slate-200 px-3 py-1 disabled:opacity-40 hover:bg-slate-50">다음</button>
          </div>
        )}
      </Card>
    </div>
  );
}


/** 변경된 키만 전/후로 보여준다 — 스냅샷 전체를 쏟으면 무엇이 바뀌었는지 되레 안 보인다. */
function Diff({ before, after }: { before: Record<string, unknown> | null;
                                   after: Record<string, unknown> | null }) {
  const b = before ?? {}, a = after ?? {};
  const keys = Array.from(new Set([...Object.keys(b), ...Object.keys(a)]))
    .filter((k) => JSON.stringify(b[k]) !== JSON.stringify(a[k]));
  if (keys.length === 0) return <span className="text-xs text-slate-400">-</span>;
  const show = (v: unknown) =>
    v === undefined ? "(없음)" : typeof v === "string" ? v : JSON.stringify(v);
  return (
    <div className="max-w-md space-y-0.5">
      {keys.slice(0, 6).map((k) => (
        <div key={k} className="text-[11px] leading-tight">
          <span className="font-medium text-slate-600">{k}</span>{" "}
          <span className="text-red-500 line-through">{show(b[k])}</span>{" → "}
          <span className="text-emerald-700">{show(a[k])}</span>
        </div>
      ))}
      {keys.length > 6 && (
        <div className="text-[11px] text-slate-400">…외 {keys.length - 6}개 항목</div>)}
    </div>
  );
}
