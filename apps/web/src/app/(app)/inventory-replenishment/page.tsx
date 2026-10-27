"use client";
/** 재고 — 안전재고 자동발주(par-level 보충·P3-7): 재주문점(안전재고) 미만 품목을 par까지 보충 제안
 *  → 선택분 1건 구매요청(PR)으로 일괄 생성. 이후 승인·발주는 PR 흐름. write=scm.po. */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { useToast } from "@/components/toast";

interface Suggestion {
  item_id: number; code: string; name: string; unit: string; current: number;
  safety_stock: number; par_level: number | null; effective_par: number;
  suggested_qty: number; is_narcotic: boolean;
}

export default function ReplenishmentPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [qty, setQty] = useState<Record<number, string>>({});

  const sug = useQuery({ queryKey: ["replenish"], queryFn: () =>
    api<{ as_of: string; count: number; items: Suggestion[] }>("/inventory/replenishment/suggestions") });

  const createPr = useMutation({
    mutationFn: () => {
      const ids = [...sel];
      const override: Record<number, number> = {};
      for (const id of ids) if (qty[id] && Number(qty[id]) > 0) override[id] = Number(qty[id]);
      return api<{ req_no: string }>("/inventory/replenishment/create-pr", { method: "POST",
        body: JSON.stringify({ item_ids: ids,
          qty_override: Object.keys(override).length ? override : null }) });
    },
    onSuccess: (r) => { toast.ok(`구매요청 생성 — ${r.req_no} (승인·발주는 구매요청 화면)`);
      setSel(new Set()); setQty({}); qc.invalidateQueries({ queryKey: ["replenish"] }); },
    onError: fail });

  const items = sug.data?.items ?? [];
  const toggle = (id: number) => setSel((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const allSel = items.length > 0 && items.every((s) => sel.has(s.item_id));
  const selCount = useMemo(() => items.filter((s) => sel.has(s.item_id)).length, [items, sel]);

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 안전재고 자동발주(보충 제안)</h1>
        <Button disabled={selCount === 0 || createPr.isPending} onClick={() => createPr.mutate()}>
          선택 {selCount}건 → 구매요청 생성</Button>
      </div>
      <p className="text-xs text-slate-500">
        현재고가 재주문점(안전재고) 미만인 품목을 보충 목표(par-level·미설정 시 안전재고×2)까지 보충
        제안합니다. 선택분을 1건 구매요청(PR)으로 만들면 이후 승인·공급사 지정·발주는 구매요청 화면에서
        진행됩니다. 수량은 직접 조정 가능.</p>

      <QueryErrorBanner queries={[sug]} />

      <Card title={`보충 제안 (${sug.data?.count ?? 0}건 · 기준 ${sug.data?.as_of ?? "—"})`} action={
        items.length > 0
          ? <button className="text-xs text-blue-600 hover:underline"
              onClick={() => setSel(allSel ? new Set() : new Set(items.map((s) => s.item_id)))}>
              {allSel ? "전체 해제" : "전체 선택"}</button>
          : undefined}>
        <Table headers={["", "품목", "현재고", "재주문점", "보충목표", "제안수량", "발주수량"]}
          rows={items.map((s) => [
            <input key="c" type="checkbox" checked={sel.has(s.item_id)}
              onChange={() => toggle(s.item_id)} />,
            <span key="n"><code className="text-xs">{s.code}</code> {s.name}
              {s.is_narcotic && <Badge key="nc" tone="red">마약류</Badge>}</span>,
            <span key="cur" className={s.current <= 0 ? "font-semibold text-red-600" : "text-amber-600"}>
              {s.current}{s.unit}</span>,
            `${s.safety_stock}${s.unit}`,
            <span key="p">{s.effective_par}{s.unit}
              {s.par_level == null && <span className="text-[11px] text-slate-400"> (기본)</span>}</span>,
            `${s.suggested_qty}${s.unit}`,
            <input key="q" inputMode="numeric" placeholder={String(s.suggested_qty)}
              value={qty[s.item_id] ?? ""} disabled={!sel.has(s.item_id)}
              onChange={(e) => setQty((q) => ({ ...q,
                [s.item_id]: e.target.value.replace(/[^0-9.]/g, "") }))}
              className="w-20 rounded border border-slate-300 px-1 py-0.5 text-xs disabled:bg-slate-100" />,
          ])} empty={sug.isFetching ? "불러오는 중…" : "보충 대상 없음 (전 품목 재주문점 이상)"} />
      </Card>
    </div>
  );
}
