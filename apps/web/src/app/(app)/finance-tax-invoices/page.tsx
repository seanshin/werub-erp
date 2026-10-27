"use client";
/** 재무 — 전자세금계산서 수취·대사 (A-1): 지급 전 증빙 통제, 과세/면세 집계(A-2 기초). */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiUpload } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";
import { useInputDialog } from "@/components/input-dialog";
import { AttachmentCell } from "@/components/attachment-cell";

interface Inv { id: number; approval_no: string; invoice_type: string; issue_date: string;
  supplier_bizno: string; supplier_name: string | null; supply_amount: number;
  vat_amount: number; total_amount: number; status: string; payable_id: number | null }
interface Ap { id: number; party: string; description: string | null; amount: number;
  paid: number; outstanding: number; status: string }

const TYPE: Record<string, string> = { TAX: "세금계산서", EXEMPT: "계산서(면세)", ZERO: "영세율" };

export default function TaxInvoicesPage() {
  const qc = useQueryClient();
  const { ask, dialog } = useInputDialog();
  const toast = useToast();
  const [status, setStatus] = useState("");
  const [month, setMonth] = useState("");
  const [page, setPage] = useState(1);
  const SIZE = 50;
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const refresh = () => { qc.invalidateQueries({ queryKey: ["taxinv"] });
    qc.invalidateQueries({ queryKey: ["taxinv-sum"] }); };
  const setStatusF = (v: string) => { setStatus(v); setPage(1); };
  const setMonthF = (v: string) => { setMonth(v); setPage(1); };

  const list = useQuery({ queryKey: ["taxinv", status, month, page], queryFn: () => {
    const p = new URLSearchParams({ page: String(page), size: String(SIZE) });
    if (status) p.set("status", status);
    if (month) p.set("month", month);
    return api<{ count: number; items: Inv[] }>(`/finance/tax-invoices?${p}`);
  } });
  const total = list.data?.count ?? 0;
  const lastPage = Math.max(1, Math.ceil(total / SIZE));
  const summary = useQuery({ queryKey: ["taxinv-sum"], queryFn: () =>
    api<{ unmatched: { count: number; total_amount: number };
      months: { month: string; invoice_type: string; supply_amount: number;
        vat_amount: number }[] }>(`/finance/tax-invoices/summary?year=${new Date().getFullYear()}`) });
  const payables = useQuery({ queryKey: ["ap-open"], queryFn: () =>
    api<{ items: Ap[] }>("/finance/payables") });

  const [form, setForm] = useState({ approval_no: "", issue_date: "", supplier_bizno: "",
    supplier_name: "", supply_amount: "", vat_amount: "0", invoice_type: "TAX" });
  const register = useMutation({
    mutationFn: () => api("/finance/tax-invoices", { method: "POST", body: JSON.stringify({
      ...form, supply_amount: Number(form.supply_amount), vat_amount: Number(form.vat_amount) }) }),
    onSuccess: () => { toast.ok("등록 완료"); refresh(); }, onError: fail });

  const upload = useMutation({
    mutationFn: (f: File) => { const fd = new FormData(); fd.append("file", f);
      return apiUpload<{ created: number; duplicated: number; failed: number;
        errors: string[] }>("/finance/tax-invoices/upload.csv", fd); },
    onSuccess: (r) => { toast.ok(`CSV — 신규 ${r.created}·중복 ${r.duplicated}·실패 ${r.failed}${
      r.errors.length ? ` (${r.errors[0]}…)` : ""}`); refresh(); }, onError: fail });

  const match = useMutation({
    mutationFn: (p: { id: number; payableId: number }) =>
      api(`/finance/tax-invoices/${p.id}/match?payable_id=${p.payableId}`, { method: "POST" }),
    onSuccess: () => { toast.ok("대사 완료"); refresh(); }, onError: fail });
  const unmatch = useMutation({
    mutationFn: (id: number) => api(`/finance/tax-invoices/${id}/unmatch`, { method: "POST" }),
    onSuccess: () => { toast.ok("대사 해제"); refresh(); }, onError: fail });

  const pickPayable = async (inv: Inv) => {
    const open = (payables.data?.items ?? []).filter((a) => a.status !== "PAID");
    const ids = new Set(open.map((a) => a.id));
    const guide = open.slice(0, 8).map((a) =>
      // 서버가 outstanding을 계산해 준다(구 코드의 a.paid_amount는 응답에 없어 NaN이었다)
      `#${a.id} ${a.party}${a.description ? ` ${a.description}` : ""} ${won(a.outstanding)}`)
      .join("  /  ");
    const v = await ask({ title: "미지급금 대사", label: "대사할 미지급금 ID",
      kind: "number", hint: guide || "미지급금 없음",
      validate: (s) => ids.has(Number(s)) ? null : "목록의 미지급금 ID를 입력하세요",
      confirmLabel: "대사" });
    if (v != null) match.mutate({ id: inv.id, payableId: Number(v) });
  };

  const input = "rounded-md border border-slate-300 px-2 py-1 text-sm";
  const un = summary.data?.unmatched;
  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">재무 — 전자세금계산서 (수취·대사)</h1>
      {un && un.count > 0 && (
        <p className="rounded-md bg-amber-50 px-3 py-1.5 text-sm text-amber-700">
          미대사 {un.count}건 · {won(un.total_amount)} — 지급 전 증빙 대사를 완료하세요
        </p>
      )}

      <Card title="등록" action={
        <label className="cursor-pointer text-xs text-blue-600 hover:underline">
          홈택스 CSV 업로드
          <input type="file" accept=".csv" className="hidden"
            onChange={(e) => e.target.files?.[0] && upload.mutate(e.target.files[0])} />
        </label>}>
        <div className="flex flex-wrap items-end gap-2">
          <input placeholder="승인번호" value={form.approval_no} className={`${input} w-44`}
            onChange={(e) => setForm({ ...form, approval_no: e.target.value })} />
          <input type="date" value={form.issue_date} className={input}
            onChange={(e) => setForm({ ...form, issue_date: e.target.value })} />
          <input placeholder="공급자 사업자번호" value={form.supplier_bizno} className={`${input} w-36`}
            onChange={(e) => setForm({ ...form, supplier_bizno: e.target.value })} />
          <input placeholder="상호" value={form.supplier_name} className={`${input} w-32`}
            onChange={(e) => setForm({ ...form, supplier_name: e.target.value })} />
          <input placeholder="공급가액" inputMode="numeric" value={form.supply_amount}
            className={`${input} w-28`}
            onChange={(e) => setForm({ ...form, supply_amount: e.target.value })} />
          <input placeholder="세액" inputMode="numeric" value={form.vat_amount}
            className={`${input} w-24`}
            onChange={(e) => setForm({ ...form, vat_amount: e.target.value })} />
          <select value={form.invoice_type} className={input}
            onChange={(e) => setForm({ ...form, invoice_type: e.target.value })}>
            {Object.entries(TYPE).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <Button disabled={!form.approval_no || !form.issue_date || !form.supply_amount || register.isPending}
            onClick={() => register.mutate()}>등록</Button>
        </div>
      </Card>

      <Card title={`수취 목록 (${list.data?.count ?? 0}건)`}>
        <div className="mb-2 flex gap-2">
          <select value={status} onChange={(e) => setStatusF(e.target.value)} className={input}>
            <option value="">전체</option><option value="RECEIVED">미대사</option>
            <option value="MATCHED">대사완료</option>
          </select>
          <input placeholder="YYYY-MM" value={month} className={`${input} w-28`}
            onChange={(e) => setMonthF(e.target.value)} />
        </div>
        <Table headers={["승인번호", "종류", "작성일", "공급자", "공급가액", "세액", "합계", "상태", "증빙", ""]}
          rows={(list.data?.items ?? []).map((t) => [
            <code key="n" className="text-xs">{t.approval_no}</code>,
            TYPE[t.invoice_type] ?? t.invoice_type, t.issue_date,
            `${t.supplier_name ?? ""} (${t.supplier_bizno})`,
            won(t.supply_amount), won(t.vat_amount), won(t.total_amount),
            t.status === "MATCHED"
              ? <Badge key="s" tone="green">대사 #{t.payable_id}</Badge>
              : <Badge key="s" tone="amber">미대사</Badge>,
            <AttachmentCell key="att" entityType="tax_invoice" entityId={t.id} />,
            t.status === "MATCHED"
              ? <Button key="b" variant="neutral" size="sm" disabled={unmatch.isPending} onClick={() => unmatch.mutate(t.id)}>해제</Button>
              : <Button key="b" size="sm" onClick={() => pickPayable(t)}>대사</Button>,
          ])} />
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

      <Card title="월별 과세/면세 매입 집계 (신고 기초)">
        <Table headers={["월", "종류", "공급가액", "세액(불공제→원가)"]}
          rows={(summary.data?.months ?? []).map((m) => [
            m.month, TYPE[m.invoice_type] ?? m.invoice_type,
            won(m.supply_amount), won(m.vat_amount)])} />
      </Card>
    </div>
  );
}
