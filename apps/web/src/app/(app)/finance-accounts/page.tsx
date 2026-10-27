"use client";
/** 계정과목(COA) 관리 — 재무제표·대시보드 분류 기반. 생성·수정·비활성(코드 불변). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Acct {
  id: number; code: string; name: string; kind: string; kind_kr: string;
  parent_id: number | null; active: boolean; in_use: boolean; cost_type: string | null;
  cost_function: string | null; is_current: boolean | null; cf_activity: string | null;
  ifrs_element: string | null;
}
const CF_KR: Record<string, string> = {
  OPERATING: "영업", INVESTING: "투자", FINANCING: "재무", "": "미분류" };
const KINDS = ["ASSET", "LIABILITY", "CAPITAL", "REVENUE", "EXPENSE"];
const KIND_KR: Record<string, string> = {
  ASSET: "자산", LIABILITY: "부채", CAPITAL: "자본", REVENUE: "수익", EXPENSE: "비용" };
const COST_KR: Record<string, string> = { FIXED: "고정비", VARIABLE: "변동비", "": "미지정" };
const FUNC_KR: Record<string, string> = { COGS: "매출원가", SGA: "판관비", OTHER: "기타", "": "미지정" };

export default function AccountsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const [kind, setKind] = useState("");
  const [form, setForm] = useState({ code: "", name: "", kind: "EXPENSE" });
  const accounts = useQuery({ queryKey: ["accounts", kind],
    queryFn: () => api<{ items: Acct[] }>(`/finance/accounts${kind ? `?kind=${kind}` : ""}`) });
  const expenses = useQuery({ queryKey: ["accounts", "EXPENSE-all"],
    queryFn: () => api<{ items: Acct[] }>("/finance/accounts?kind=EXPENSE") });
  const unclassified = (expenses.data?.items ?? []).filter((a) => a.active && !a.cost_type);
  const unclassifiedInUse = unclassified.filter((a) => a.in_use);
  const coverage = useQuery({ queryKey: ["accounts", "ifrs-coverage"],
    queryFn: () => api<{ missing_is_current: { count: number }; missing_cf_activity: { count: number };
      missing_ifrs_element: { count: number; total: number } }>(
      "/finance/accounts/classification-coverage") });
  const refresh = () => qc.invalidateQueries({ queryKey: ["accounts"] });
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");

  const create = useMutation({
    mutationFn: () => api("/finance/accounts", { method: "POST", body: JSON.stringify(form) }),
    onSuccess: () => { toast.ok(`계정 ${form.code} 생성`); setForm({ code: "", name: "", kind: "EXPENSE" }); refresh(); },
    onError: fail });
  const update = useMutation({
    mutationFn: (p: { id: number; patch: { name?: string; kind?: string; active?: boolean;
      cost_type?: string; clear_cost_type?: boolean;
      cost_function?: string; clear_cost_function?: boolean;
      is_current?: boolean; clear_is_current?: boolean;
      cf_activity?: string; clear_cf_activity?: boolean;
      ifrs_element?: string; clear_ifrs_element?: boolean } }) =>
      api(`/finance/accounts/${p.id}`, { method: "PUT", body: JSON.stringify(p.patch) }),
    onSuccess: () => { toast.ok("수정 완료"); refresh(); }, onError: fail });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">계정과목 관리</h1>
      {unclassified.length > 0 && (
        <div className="rounded-lg border border-amber-300 bg-amber-50/60 p-3 text-sm text-amber-700">
          ⚠ 원가구분 미지정 비용 계정 <b>{unclassified.length}건</b>
          (사용중 <b>{unclassifiedInUse.length}건</b>) — 대시보드 고정/변동 집계에서 누락됩니다.
          비용 행의 <b>원가구분</b>을 고정/변동으로 지정하세요.
        </div>
      )}
      {coverage.data && (coverage.data.missing_is_current.count > 0 || coverage.data.missing_cf_activity.count > 0 || coverage.data.missing_ifrs_element.count > 0) && (
        <div className="rounded-lg border border-sky-300 bg-sky-50/60 p-3 text-sm text-sky-800">
          ℹ IFRS 분류 미지정 — 유동 <b>{coverage.data.missing_is_current.count}건</b>(G8)·현금흐름 <b>{coverage.data.missing_cf_activity.count}건</b>(G8)·
          {" "}IFRS element <b>{coverage.data.missing_ifrs_element.count}</b>/<b>{coverage.data.missing_ifrs_element.total}건</b>(G10).
          미지정은 코드 규칙(폴백)으로 처리되나, 각 열에서 직접 지정하면 데이터 주도 분류·XBRL 태깅이 완결됩니다.
          {" "}<span className="text-xs">(XBRL 전자공시 생성기는 규제 확인 후 별도 구축)</span>
          {unclassifiedInUse.length > 0 && (
            <span className="ml-1 text-amber-600">
              ({unclassifiedInUse.slice(0, 5).map((a) => a.code).join(", ")}{unclassifiedInUse.length > 5 ? " …" : ""})
            </span>)}
        </div>
      )}

      <Card title="계정 추가">
        <div className="flex flex-wrap items-end gap-2">
          <Input label="코드" value={form.code} onChange={(e) => setForm({ ...form, code: e.target.value })} className="w-28" />
          <Input label="명칭" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} className="min-w-48" />
          <label className="text-sm">종류
            <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}
              className="ml-2 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              {KINDS.map((k) => <option key={k} value={k}>{KIND_KR[k]}</option>)}
            </select>
          </label>
          <Button disabled={!form.code || !form.name || create.isPending} onClick={() => create.mutate()}>추가</Button>
        </div>
        <p className="mt-2 text-xs text-slate-400">코드는 생성 후 변경 불가(전표 집계 보존). 삭제 대신 비활성 처리하세요.</p>
      </Card>

      <Card title="계정과목" action={
        <select value={kind} onChange={(e) => setKind(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-sm">
          <option value="">전체 종류</option>
          {KINDS.map((k) => <option key={k} value={k}>{KIND_KR[k]}</option>)}
        </select>}>
        <Table sortable pageSize={30} stickyHeader maxHeight={600}
          headers={["코드", "명칭", "종류", "원가구분", "기능구분", "유동", "현금흐름", "IFRS element", "사용", "상태", ""]}
          rows={(accounts.data?.items ?? []).map((a) => [
            a.code,
            <EditableName key="n" a={a} onSave={(name) => update.mutate({ id: a.id, patch: { name } })} />,
            <KindSelect key="k" a={a} onSave={(k) => update.mutate({ id: a.id, patch: { kind: k } })} />,
            a.kind === "EXPENSE"
              ? <CostSelect key="c" a={a} onSave={(v) => update.mutate({ id: a.id,
                  patch: v ? { cost_type: v } : { clear_cost_type: true } })} />
              : <span key="c" className="text-xs text-slate-300">—</span>,
            a.kind === "EXPENSE"
              ? <FunctionSelect key="f" a={a} onSave={(v) => update.mutate({ id: a.id,
                  patch: v ? { cost_function: v } : { clear_cost_function: true } })} />
              : <span key="f" className="text-xs text-slate-300">—</span>,
            a.kind === "ASSET" || a.kind === "LIABILITY"
              ? <CurrentSelect key="cur" a={a} onSave={(v) => update.mutate({ id: a.id,
                  patch: v === "" ? { clear_is_current: true } : { is_current: v === "current" } })} />
              : <span key="cur" className="text-xs text-slate-300">—</span>,
            a.kind === "REVENUE" || a.kind === "EXPENSE"
              ? <span key="cf" className="text-xs text-slate-300">—</span>
              : <CfSelect key="cf" a={a} onSave={(v) => update.mutate({ id: a.id,
                  patch: v ? { cf_activity: v } : { clear_cf_activity: true } })} />,
            <ElementCell key="el" a={a} onSave={(v) => update.mutate({ id: a.id,
              patch: v ? { ifrs_element: v } : { clear_ifrs_element: true } })} />,
            a.in_use ? <Badge key="u" tone="blue">사용중</Badge> : <span key="u" className="text-xs text-slate-400">미사용</span>,
            a.active ? <Badge key="s" tone="green">활성</Badge> : <Badge key="s" tone="gray">비활성</Badge>,
            <button key="t" onClick={() => update.mutate({ id: a.id, patch: { active: !a.active } })}
              className="text-xs font-medium text-blue-600 hover:underline">{a.active ? "비활성화" : "활성화"}</button>,
          ])} empty="계정과목 없음" />
      </Card>
    </div>
  );
}

function EditableName({ a, onSave }: { a: { name: string }; onSave: (name: string) => void }) {
  const [v, setV] = useState(a.name);
  return (
    <span className="flex items-center gap-1">
      <input value={v} onChange={(e) => setV(e.target.value)}
        className="w-40 rounded border border-slate-200 px-1 py-0.5 text-sm" />
      {v !== a.name && <button onClick={() => onSave(v)} className="text-xs text-blue-600">저장</button>}
    </span>
  );
}

function KindSelect({ a, onSave }: { a: { kind: string }; onSave: (kind: string) => void }) {
  return (
    <select value={a.kind} onChange={(e) => onSave(e.target.value)}
      className="rounded border border-slate-200 px-1 py-0.5 text-sm">
      {KINDS.map((k) => <option key={k} value={k}>{KIND_KR[k]}</option>)}
    </select>
  );
}

function CostSelect({ a, onSave }: { a: { cost_type: string | null }; onSave: (v: string) => void }) {
  return (
    <select value={a.cost_type ?? ""} onChange={(e) => onSave(e.target.value)}
      className="rounded border border-slate-200 px-1 py-0.5 text-sm" title="대시보드 고정비 분류">
      {Object.entries(COST_KR).map(([v, lbl]) => <option key={v} value={v}>{lbl}</option>)}
    </select>
  );
}

function FunctionSelect({ a, onSave }: { a: { cost_function: string | null }; onSave: (v: string) => void }) {
  return (
    <select value={a.cost_function ?? ""} onChange={(e) => onSave(e.target.value)}
      className="rounded border border-slate-200 px-1 py-0.5 text-sm" title="기능별 손익(매출원가/판관비)">
      {Object.entries(FUNC_KR).map(([v, lbl]) => <option key={v} value={v}>{lbl}</option>)}
    </select>
  );
}

function CurrentSelect({ a, onSave }: { a: { is_current: boolean | null }; onSave: (v: string) => void }) {
  const val = a.is_current == null ? "" : a.is_current ? "current" : "noncurrent";
  return (
    <select value={val} onChange={(e) => onSave(e.target.value)}
      className="rounded border border-slate-200 px-1 py-0.5 text-sm" title="재무상태표 유동/비유동 분류(G8)">
      <option value="">미분류</option>
      <option value="current">유동</option>
      <option value="noncurrent">비유동</option>
    </select>
  );
}

function ElementCell({ a, onSave }: {
  a: { ifrs_element: string | null }; onSave: (v: string) => void }) {
  const [v, setV] = useState(a.ifrs_element ?? "");
  return (
    <input value={v} onChange={(e) => setV(e.target.value)}
      onBlur={() => { if (v !== (a.ifrs_element ?? "")) onSave(v); }}
      placeholder="ifrs-full:…" title="표준 IFRS 택소노미 concept(G10 기초)"
      className="w-44 rounded border border-slate-200 px-1 py-0.5 text-xs" />
  );
}

function CfSelect({ a, onSave }: { a: { cf_activity: string | null }; onSave: (v: string) => void }) {
  return (
    <select value={a.cf_activity ?? ""} onChange={(e) => onSave(e.target.value)}
      className="rounded border border-slate-200 px-1 py-0.5 text-sm" title="현금흐름 활동 분류(영업/투자/재무·G8)">
      {Object.entries(CF_KR).map(([v, lbl]) => <option key={v} value={v}>{lbl}</option>)}
    </select>
  );
}
