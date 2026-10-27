"use client";
/** 재무 — 고정자산·의료장비 (FR-09 확장): 등록·감가상각 실행. */
import { todayLocal, monthLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useInputDialog } from "@/components/input-dialog";
import { won } from "@/lib/format";

interface Asset {
  id: number; asset_no: string; name: string; category: string;
  acquired_on: string; acquisition_cost: number; accumulated_depreciation: number;
  book_value: number; useful_life_years: number; method: string; status: string;
}

interface IntangibleRow {
  id: number; asset_no: string; name: string; cost: number; capitalized_borrowing: number;
  accumulated_impairment: number; book_value: number; useful_life_years: number; status: string;
}

const CATEGORY: Record<string, string> = {
  MEDICAL: "의료장비", FIXTURE: "비품", BUILDING: "건물", VEHICLE: "차량", OTHER: "기타",
};

export default function AssetsPage() {
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const [period, setPeriod] = useState(monthLocal());
  const [form, setForm] = useState({
    asset_no: "", name: "", category: "MEDICAL", acquired_on: "",
    acquisition_cost: "", useful_life_years: "5", salvage_value: "0", method: "STRAIGHT",
    payment_method: "PAYABLE",
  });
  const toast = useToast();

  const list = useQuery({ queryKey: ["assets"],
    queryFn: () => api<{ items: Asset[] }>("/finance/assets") });

  const register = useMutation({
    mutationFn: () => api("/finance/assets", { method: "POST", body: JSON.stringify({
      ...form, acquisition_cost: Number(form.acquisition_cost),
      useful_life_years: Number(form.useful_life_years),
      salvage_value: Number(form.salvage_value) }) }),
    onSuccess: () => { toast.ok("등록 완료"); qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "등록 실패"),
  });

  const depreciate = useMutation({
    mutationFn: () => api<{ depreciated: number }>(
      `/finance/assets/depreciation/${period}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(`${period} 감가상각 ${r.depreciated}건 처리`);
      qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "감가상각 실패"),
  });

  const dispose = useMutation({
    mutationFn: (v: { id: number; amount: number }) =>
      api<{ gain_loss: number }>(
        `/finance/assets/${v.id}/dispose?disposed_on=${todayLocal()}&disposal_amount=${v.amount}`,
        { method: "POST" }),
    onSuccess: (r) => { toast.ok(`처분 완료 — 처분손익 ${won(r.gain_loss)}`);
      qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "처분 실패"),
  });

  const impair = useMutation({
    mutationFn: (v: { id: number; recoverable: number }) =>
      api<{ impairment_loss: number; book_after: number }>(
        `/finance/assets/${v.id}/impair?recoverable_amount=${v.recoverable}&on_date=${todayLocal()}`,
        { method: "POST" }),
    onSuccess: (r) => { toast.ok(`손상차손 ${won(r.impairment_loss)} 인식 — 장부가 ${won(r.book_after)}`);
      qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "손상 실패"),
  });
  const onImpair = async (a: Asset) => {
    const r = await ask({ title: "손상 — 회수가능액",
      label: `'${a.name}' 회수가능액 (장부가 ${won(a.book_value)})`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "손상 인식" });
    if (r != null) impair.mutate({ id: a.id, recoverable: Number(r) });
  };
  const revalue = useMutation({
    mutationFn: (v: { id: number; fair: number }) =>
      api<{ delta: number; book_after: number }>(
        `/finance/assets/${v.id}/revalue?fair_value=${v.fair}&on_date=${todayLocal()}`,
        { method: "POST" }),
    onSuccess: (r) => { toast.ok(`재평가 ${r.delta >= 0 ? "증가" : "감소"} ${won(Math.abs(r.delta))} — 장부가 ${won(r.book_after)}`);
      qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "재평가 실패"),
  });
  const onRevalue = async (a: Asset) => {
    const f = await ask({ title: "재평가 — 공정가치",
      label: `'${a.name}' 공정가치 (장부가 ${won(a.book_value)})`,
      kind: "number", min: 0, defaultValue: String(a.book_value), confirmLabel: "재평가" });
    if (f != null) revalue.mutate({ id: a.id, fair: Number(f) });
  };

  const intangibles = useQuery({ queryKey: ["intangibles"],
    queryFn: () => api<{ items: IntangibleRow[] }>("/finance/intangibles") });
  const intRefresh = () => qc.invalidateQueries({ queryKey: ["intangibles"] });
  const createIntangible = useMutation({
    mutationFn: (v: { asset_no: string; name: string; cost: number; life: number }) =>
      api("/finance/intangibles", { method: "POST", body: JSON.stringify({
        asset_no: v.asset_no, name: v.name, acquired_on: todayLocal(),
        cost: v.cost, useful_life_years: v.life }) }),
    onSuccess: () => { toast.ok("무형자산 취득 등록"); intRefresh(); }, onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const amortizeInt = useMutation({
    mutationFn: (id: number) => api(`/finance/intangibles/${id}/amortize/${monthLocal()}`, { method: "POST" }),
    onSuccess: () => { toast.ok("무형자산 상각"); intRefresh(); }, onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const today = () => todayLocal();
  const impairInt = useMutation({
    mutationFn: (v: { id: number; recoverable: number }) =>
      api(`/finance/intangibles/${v.id}/impair?recoverable_amount=${v.recoverable}&on_date=${today()}`, { method: "POST" }),
    onSuccess: () => { toast.ok("무형자산 손상 인식"); intRefresh(); }, onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const reverseImpairInt = useMutation({
    mutationFn: (v: { id: number; recoverable: number }) =>
      api(`/finance/intangibles/${v.id}/reverse-impairment?recoverable_amount=${v.recoverable}&on_date=${today()}`, { method: "POST" }),
    onSuccess: () => { toast.ok("무형자산 손상환입"); intRefresh(); }, onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const disposeInt = useMutation({
    mutationFn: (v: { id: number; amount: number }) =>
      api(`/finance/intangibles/${v.id}/dispose?disposed_on=${today()}&disposal_amount=${v.amount}`, { method: "POST" }),
    onSuccess: () => { toast.ok("무형자산 처분"); intRefresh(); }, onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "실패"),
  });
  const onImpairInt = async (x: IntangibleRow) => {
    const r = await ask({ title: "무형자산 손상 — 회수가능액 (K-IFRS 1036)",
      label: `'${x.name}' 회수가능액 (장부가 ${won(x.book_value)})`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "손상 인식" });
    if (r != null) impairInt.mutate({ id: x.id, recoverable: Number(r) });
  };
  const onReverseImpairInt = async (x: IntangibleRow) => {
    const r = await ask({ title: "무형자산 손상환입 — 회수가능액 (K-IFRS 1036)",
      label: `'${x.name}' 회복 회수가능액 (손상누계 ${won(x.accumulated_impairment)} 한도)`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "손상환입" });
    if (r != null) reverseImpairInt.mutate({ id: x.id, recoverable: Number(r) });
  };
  const onDisposeInt = async (x: IntangibleRow) => {
    const amt = await ask({ title: "무형자산 처분 — 처분가 (K-IFRS 1038)",
      label: `'${x.name}' 처분가 (장부가 ${won(x.book_value)})`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "처분" });
    if (amt != null) disposeInt.mutate({ id: x.id, amount: Number(amt) });
  };
  const onAddIntangible = async () => {
    const no = await ask({ title: "무형자산 취득", label: "자산번호", kind: "text", confirmLabel: "다음" });
    if (!no) return;
    const nm = await ask({ title: "무형자산 취득", label: "자산명", kind: "text", confirmLabel: "다음" });
    if (!nm) return;
    const cost = await ask({ title: "무형자산 취득", label: "취득원가(원)", kind: "number", min: 0, defaultValue: "0", confirmLabel: "다음" });
    if (!cost) return;
    const life = await ask({ title: "무형자산 취득", label: "내용연수(년)", kind: "number", min: 1, defaultValue: "5", confirmLabel: "등록" });
    if (life) createIntangible.mutate({ asset_no: String(no), name: String(nm), cost: Number(cost), life: Number(life) });
  };

  const reverseImpair = useMutation({
    mutationFn: (v: { id: number; recoverable: number }) =>
      api<{ reversal: number; book_after: number }>(
        `/finance/assets/${v.id}/reverse-impairment?recoverable_amount=${v.recoverable}&on_date=${todayLocal()}`,
        { method: "POST" }),
    onSuccess: (r) => { toast.ok(`손상환입 ${won(r.reversal)} — 장부가 ${won(r.book_after)}`);
      qc.invalidateQueries({ queryKey: ["assets"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "손상환입 실패"),
  });
  const onReverseImpair = async (a: Asset) => {
    const r = await ask({ title: "손상환입 — 회수가능액 (K-IFRS 1036)",
      label: `'${a.name}' 회복된 회수가능액 (손상누계 한도 내 환입)`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "손상환입" });
    if (r != null) reverseImpair.mutate({ id: a.id, recoverable: Number(r) });
  };

  const onDispose = async (a: Asset) => {
    const amt = await ask({ title: "처분 — 처분가",
      label: `'${a.name}' 처분가 (장부가 ${won(a.book_value)})`,
      kind: "number", min: 0, defaultValue: "0", confirmLabel: "처분" });
    if (amt != null) dispose.mutate({ id: a.id, amount: Number(amt) });
  };

  const num = (n: number) => n.toLocaleString();
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setForm({ ...form, [k]: e.target.value });

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">재무 — 고정자산·의료장비</h1>

      <Card title="자산 등록 (취득 전표 자동생성)">
        <div className="grid gap-2 sm:grid-cols-3">
          <input placeholder="자산번호" value={form.asset_no} onChange={set("asset_no")}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <input placeholder="자산명" value={form.name} onChange={set("name")}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <select value={form.category} onChange={set("category")}
            className="rounded border border-slate-300 px-2 py-1 text-sm">
            {Object.entries(CATEGORY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <input type="date" value={form.acquired_on} onChange={set("acquired_on")}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <input type="number" placeholder="취득가" value={form.acquisition_cost}
            onChange={set("acquisition_cost")}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <input type="number" placeholder="내용연수(년)" value={form.useful_life_years}
            onChange={set("useful_life_years")}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <select value={form.method} onChange={set("method")}
            className="rounded border border-slate-300 px-2 py-1 text-sm">
            <option value="STRAIGHT">정액법</option>
            <option value="DECLINING">정률법</option>
          </select>
          <select value={form.payment_method} onChange={set("payment_method")}
            className="rounded border border-slate-300 px-2 py-1 text-sm">
            <option value="PAYABLE">미지급</option>
            <option value="CASH">현금</option>
          </select>
          <Button onClick={() => register.mutate()} disabled={register.isPending}>등록</Button>
        </div>
      </Card>

      <Card title="월 감가상각 실행 (감가상각비/누계액 전표 생성)">
        <div className="flex items-center gap-2">
          <input type="month" value={period} onChange={(e) => setPeriod(e.target.value)}
            className="rounded border border-slate-300 px-2 py-1 text-sm" />
          <Button variant="neutral" onClick={() => depreciate.mutate()} disabled={depreciate.isPending}>감가상각 실행</Button>
        </div>
      </Card>

      <Card title="자산 목록">
        <Table headers={["자산번호", "자산명", "분류", "취득가", "감가누계", "장부가", "상태", ""]}
          rows={(list.data?.items ?? []).map((a) => [
            a.asset_no, a.name, CATEGORY[a.category] ?? a.category,
            num(a.acquisition_cost), num(a.accumulated_depreciation), num(a.book_value),
            <Badge key="s" tone={a.status === "IN_USE" ? "green" : "amber"}>
              {a.status === "IN_USE" ? "사용중" : "처분"}</Badge>,
            a.status === "IN_USE"
              ? <span key="d" className="flex gap-1">
                  <Button size="sm" onClick={() => onRevalue(a)}>재평가</Button>
                  <Button variant="warning" size="sm" onClick={() => onImpair(a)}>손상</Button>
                  <Button variant="success" size="sm" onClick={() => onReverseImpair(a)}>손상환입</Button>
                  <Button variant="neutral" size="sm" onClick={() => onDispose(a)}>처분</Button>
                </span>
              : "—",
          ])} />
      </Card>

      <Card title="무형자산 (K-IFRS 1038·차입원가 1023)" action={
        <Button size="sm" onClick={onAddIntangible}>무형자산 취득</Button>}>
        <Table headers={["자산번호", "자산명", "취득원가", "손상누계", "장부가", "내용연수", ""]}
          rows={(intangibles.data?.items ?? []).map((x) => [
            x.asset_no, x.name, num(x.cost), num(x.accumulated_impairment), num(x.book_value),
            `${x.useful_life_years}년`,
            x.status === "IN_USE"
              ? <span key="a" className="flex flex-wrap gap-1">
                  <Button size="sm" onClick={() => amortizeInt.mutate(x.id)}>월 상각</Button>
                  <Button variant="warning" size="sm" onClick={() => onImpairInt(x)}>손상</Button>
                  {x.accumulated_impairment > 0 && (
                    <Button variant="success" size="sm" onClick={() => onReverseImpairInt(x)}>환입</Button>)}
                  <Button variant="neutral" size="sm" onClick={() => onDisposeInt(x)}>처분</Button>
                </span>
              : <span key="a" className="text-xs text-slate-400">처분됨</span>,
          ])} empty={intangibles.isFetching ? "불러오는 중…" : "무형자산 없음"} />
      </Card>
    </div>
  );
}
