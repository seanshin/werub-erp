"use client";
/** 재고 — 입고 반품(RMA): 발주 선택 → 반품 로트·수량·사유 → credit note(DR 2110/CR 1150)·
 *  미지급 차감·교체 재입고 재개. 반품은 로트 잔량 이내. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryErrorBanner } from "@/components/query-guard";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { won } from "@/lib/format";
import { useToast } from "@/components/toast";

const REASONS: Record<string, string> = {
  DEFECT: "불량", DAMAGE: "파손", EXPIRY: "유효기간", WRONG_ITEM: "오배송", OTHER: "기타",
};
const RECEIVED = new Set(["RECEIVED", "PARTIAL"]);

interface PoLine { item_id: number; qty: number; unit_price: number }
interface Po { id: number; supplier_id: number; status: string; requested_at: string; lines: PoLine[] }
interface Supplier { id: number; name: string }
interface Lot {
  id: number; item_id: number; item_name: string; lot_no: string;
  remaining_qty: number; unit_cost: number | null; expiry_on: string | null;
}
interface Ret {
  id: number; po_id: number; item_id: number; lot_id: number; qty: number; amount: number;
  reason: string; returned_on: string; credit_voucher_ref: string | null;
}

export default function PurchaseReturnsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [poId, setPoId] = useState<number | null>(null);
  const [returning, setReturning] = useState(false);

  // v1.203 — 서버 status 필터: 최신 100건 클라 필터로 오래된 입고 발주가 조용히 누락되던 결함
  const pos = useQuery({ queryKey: ["po-board", "received"], queryFn: () =>
    api<{ items: Po[] }>("/inventory/purchase-orders?size=200&status=RECEIVED,PARTIAL") });
  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: () =>
    api<{ items: Supplier[] }>("/inventory/suppliers") });
  const returns = useQuery({ enabled: poId != null, queryKey: ["po-returns", poId], queryFn: () =>
    api<{ items: Ret[] }>(`/inventory/purchase-orders/${poId}/returns`) });
  const lots = useQuery({ enabled: returning && poId != null, queryKey: ["returnable-lots", poId], queryFn: () =>
    api<{ items: Lot[] }>(`/inventory/purchase-orders/${poId}/returnable-lots`) });

  const supplierName = (id: number) => suppliers.data?.items.find((s) => s.id === id)?.name ?? `#${id}`;
  const received = (pos.data?.items ?? []).filter((p) => RECEIVED.has(p.status));

  const create = useMutation({
    mutationFn: (b: { lot_id: number; qty: number; reason: string; returned_on: string; note: string }) =>
      api<{ amount: number }>(`/inventory/purchase-orders/${poId}/returns`, { method: "POST",
        body: JSON.stringify(b) }),
    onSuccess: (r) => { toast.ok(`반품 처리 — 재고 반제 ${won(r.amount)} (credit note)`);
      setReturning(false);
      qc.invalidateQueries({ queryKey: ["po-returns"] });
      qc.invalidateQueries({ queryKey: ["po-board"] });
      qc.invalidateQueries({ queryKey: ["returnable-lots"] }); },
    onError: fail,
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재고 — 입고 반품(RMA)</h1>
      <QueryErrorBanner queries={[pos, returns]} />
      <p className="text-xs text-slate-500">
        불량·파손·유효기간·오배송 입고분을 공급사에 반품. 로트 잔량 차감·credit note(재고↓·미지급↓)·
        교체 재입고 재개(3-way 여유 확보). 소비·폐기분은 반품 불가(잔량 이내).</p>

      <Card title={`입고 완료 발주 (${received.length})`} action={
        <span className="text-[11px] text-slate-400">RECEIVED·PARTIAL만 반품 가능</span>}>
        <Table headers={["발주#", "공급사", "상태", "요청일", "품목 수", ""]}
          rows={received.map((p) => [
            `#${p.id}`, supplierName(p.supplier_id),
            <Badge key="s" tone={p.status === "RECEIVED" ? "green" : "amber"}>{p.status}</Badge>,
            p.requested_at?.slice(0, 10) ?? "—", `${p.lines.length}종`,
            <Button key="v" variant="secondary" size="sm"
              onClick={() => setPoId(p.id)}>반품 내역</Button>,
          ])} empty={pos.isFetching ? "불러오는 중…" : "입고 완료 발주 없음"} />
      </Card>

      {poId != null && (
        <Card title={`발주 #${poId} 반품 내역`} action={
          <div className="flex gap-1.5">
            <Button size="sm" onClick={() => setReturning(true)}>반품 등록</Button>
            <Button variant="secondary" size="sm" onClick={() => setPoId(null)}>닫기</Button>
          </div>}>
          <Table headers={["반품#", "품목", "로트", "수량", "반제액", "사유", "반품일", "전표"]}
            rows={(returns.data?.items ?? []).map((r) => [
              `#${r.id}`, `#${r.item_id}`, `#${r.lot_id}`, r.qty, won(r.amount),
              <Badge key="rs" tone="gray">{REASONS[r.reason] ?? r.reason}</Badge>,
              r.returned_on, r.credit_voucher_ref ?? "—",
            ])} empty={returns.isFetching ? "불러오는 중…" : "반품 내역 없음"} />
        </Card>
      )}

      {returning && poId != null && (
        <Modal title={`발주 #${poId} 입고 반품`} onClose={() => setReturning(false)}>
          <ReturnForm lots={lots.data?.items ?? []} loadingLots={lots.isFetching}
            isLoading={create.isPending} onSubmit={(b) => create.mutate(b)} />
        </Modal>
      )}
    </div>
  );
}

function ReturnForm({ lots, loadingLots, onSubmit, isLoading }: {
  lots: Lot[]; loadingLots: boolean;
  onSubmit: (b: { lot_id: number; qty: number; reason: string; returned_on: string; note: string }) => void;
  isLoading: boolean;
}) {
  const [lotId, setLotId] = useState("");
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("DEFECT");
  const [returnedOn, setReturnedOn] = useState(todayLocal());
  const [note, setNote] = useState("");
  const lot = lots.find((l) => l.id === Number(lotId));
  const n = Number(qty) || 0;
  const over = lot != null && n > lot.remaining_qty;
  const ok = lot != null && n > 0 && !over;
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">반품 로트
        <select value={lotId} onChange={(e) => setLotId(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">{loadingLots ? "로트 조회 중…" : "— 선택 —"}</option>
          {lots.map((l) => (
            <option key={l.id} value={l.id}>
              {l.item_name} · {l.lot_no} (잔량 {l.remaining_qty})</option>))}
        </select>
      </label>
      {lots.length === 0 && !loadingLots && (
        <p className="text-xs text-amber-700">반품 가능한 로트가 없습니다(잔량 소진).</p>)}
      <div className="grid grid-cols-2 gap-2">
        <Input label={`반품 수량${lot ? ` (잔량 ${lot.remaining_qty})` : ""}`} value={qty} inputMode="numeric"
          onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
        <label className="block text-sm text-slate-600">사유
          <select value={reason} onChange={(e) => setReason(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(REASONS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
      </div>
      {lot != null && n > 0 && (
        <p className="text-xs text-slate-500">
          재고 반제 예상 {won(Math.round(n * (lot.unit_cost ?? 0)))} (장부단가 기준)</p>)}
      {over && <p className="text-xs text-red-600">잔량({lot?.remaining_qty})을 초과할 수 없습니다</p>}
      <Input label="반품일" type="date" value={returnedOn}
        onChange={(e) => setReturnedOn(e.target.value)} />
      <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
      <Button disabled={isLoading || !ok}
        onClick={() => onSubmit({ lot_id: Number(lotId), qty: n, reason,
          returned_on: returnedOn, note })}>반품 등록</Button>
    </div>
  );
}
