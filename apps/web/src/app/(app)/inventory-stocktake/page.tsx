"use client";
/** 재고 — 실사(stock-taking): 실재고 입력→차이 산정→로트 조정·감모손실/조정이익 전표. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { won } from "@/lib/format";
import { Badge, Button, Card, Table } from "@/components/ui";
import { useToast } from "@/components/toast";

interface Take { id: number; item_id: number; code: string; name: string; taken_on: string;
  book_qty: number; physical_qty: number; diff_qty: number; diff_value: number; status: string }


export default function StockTakePage() {
  const qc = useQueryClient();
  const [f, setF] = useState({ item_id: "", physical_qty: "", taken_on: todayLocal() });
  const toast = useToast();
  const takes = useQuery({ queryKey: ["stocktakes"], queryFn: () =>
    api<{ items: Take[] }>("/inventory/stock-takes") });

  const record = useMutation({
    mutationFn: () => api<{ id: number; diff_qty: number; diff_value: number }>("/inventory/stock-takes", {
      method: "POST", body: JSON.stringify({ item_id: Number(f.item_id),
        physical_qty: Number(f.physical_qty), taken_on: f.taken_on }) }),
    onSuccess: (d) => { toast.ok(`실사 기록 #${d.id} — 차이 ${d.diff_qty} (${won(d.diff_value)}). 검토 후 반영하세요`);
      qc.invalidateQueries({ queryKey: ["stocktakes"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "기록 실패"),
  });
  const apply = useMutation({
    mutationFn: (id: number) => api(`/inventory/stock-takes/${id}/apply`, { method: "POST" }),
    onSuccess: () => { toast.ok("실사 반영 완료 — 재고 조정·전표 생성"); qc.invalidateQueries({ queryKey: ["stocktakes"] }); },
    onError: (e) => toast.err(e instanceof ApiError ? e.error.message : "반영 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재고 — 실사(stock-taking) 조정</h1>

      <Card title="실사 기록" action={
        <span className="text-[11px] text-slate-400">부족=감모손실 · 과잉=조정이익 전표</span>}>
        <div className="flex flex-wrap items-center gap-2">
          <input value={f.item_id} onChange={(e) => setF({ ...f, item_id: e.target.value })}
            placeholder="품목 ID" className="w-28 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input value={f.physical_qty} onChange={(e) => setF({ ...f, physical_qty: e.target.value })}
            placeholder="실재고 수량" className="w-32 rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <input type="date" value={f.taken_on} onChange={(e) => setF({ ...f, taken_on: e.target.value })}
            className="rounded-md border border-slate-300 px-3 py-1.5 text-sm" />
          <Button disabled={record.isPending}
            onClick={() => f.item_id && f.physical_qty ? record.mutate() : toast.err("품목과 수량을 입력하세요")}>실사 기록</Button>
        </div>
      </Card>

      <Card title="실사 내역">
        <Table headers={["품목", "장부", "실재고", "차이", "차액", "상태", ""]}
          rows={(takes.data?.items ?? []).map((t) => [
            <span key="n">{t.name} <code className="text-xs text-slate-400">#{t.item_id}</code></span>,
            t.book_qty.toLocaleString(), t.physical_qty.toLocaleString(),
            <b key="d" className={t.diff_qty < 0 ? "text-red-600" : t.diff_qty > 0 ? "text-emerald-700" : ""}>
              {t.diff_qty > 0 ? "+" : ""}{t.diff_qty}</b>,
            won(t.diff_value),
            <Badge key="s" tone={t.status === "APPLIED" ? "green" : "amber"}>
              {t.status === "APPLIED" ? "반영" : "검토"}</Badge>,
            t.status === "DRAFT"
              ? <Button key="a" size="sm" disabled={apply.isPending} onClick={() => apply.mutate(t.id)}>반영</Button>
              : "",
          ])} empty={takes.isFetching ? "불러오는 중…" : "실사 내역 없음"} />
      </Card>
    </div>
  );
}
