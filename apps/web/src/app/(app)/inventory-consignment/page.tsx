"use client";
/** 재고 — 위탁재고(Consignment): 공급사 소유 상태로 보관하다 사용 시점 매입(DR5410/CR2110).
 *  적재(무회계)·사용(매입인식·client_ref 멱등)·반품(무회계)·사용 내역 집계. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryErrorBanner } from "@/components/query-guard";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { won } from "@/lib/format";
import { useToast } from "@/components/toast";

interface Stock {
  id: number; supplier_id: number; item_id: number; lot_no: string; expiry_on: string | null;
  placed_qty: number; qty_on_hand: number; unit_cost: number; status: string;
}
interface Supplier { id: number; name: string; business_no: string | null; status: string }
interface Item { id: number; code: string; name: string }
interface Consumption {
  items: { id: number; consignment_id: number; qty: number; amount: number;
    // chart_masked=true면 서버가 마스킹한 값(조달 스코프에 환자 특정 정보를 주지 않는다)
    consumed_on: string; chart_no: string | null; chart_masked?: boolean;
    payable_ref: string | null }[];
  total_amount: number;
}
function uuid(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
}
const STATUS_TONE: Record<string, "green" | "gray" | "amber"> = {
  ACTIVE: "green", DEPLETED: "gray", RETURNED: "amber",
};

export default function ConsignmentPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [statusFilter, setStatusFilter] = useState("ACTIVE");
  const [placing, setPlacing] = useState(false);
  const [act, setAct] = useState<{ stock: Stock; mode: "consume" | "return" } | null>(null);
  const [since, setSince] = useState(todayLocal().slice(0, 8) + "01");
  const [until, setUntil] = useState(todayLocal());

  const stock = useQuery({ queryKey: ["consign-stock", statusFilter], queryFn: () =>
    api<{ items: Stock[] }>(`/inventory/consignment?status=${statusFilter}`) });
  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: () =>
    api<{ items: Supplier[] }>("/inventory/suppliers") });
  const items = useQuery({ queryKey: ["scm-items"], queryFn: () =>
    api<{ items: Item[] }>("/inventory/items?size=200") });
  const usage = useQuery({ queryKey: ["consign-usage", since, until], queryFn: () =>
    api<Consumption>(`/inventory/consignment-consumptions?since=${since}&until=${until}`) });

  const supplierName = (id: number) => suppliers.data?.items.find((s) => s.id === id)?.name ?? `#${id}`;
  const itemName = (id: number) => items.data?.items.find((i) => i.id === id)?.name ?? `#${id}`;
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["consign-stock"] });
    qc.invalidateQueries({ queryKey: ["consign-usage"] });
  };

  const place = useMutation({
    mutationFn: (b: Record<string, unknown>) =>
      api("/inventory/consignment", { method: "POST", body: JSON.stringify(b) }),
    onSuccess: () => { toast.ok("위탁재고 적재 완료"); setPlacing(false); refresh(); },
    onError: fail,
  });
  const consume = useMutation({
    mutationFn: (v: { id: number; qty: number; consumed_on: string; chart_no: string; client_ref: string }) =>
      api<{ amount: number }>(`/inventory/consignment/${v.id}/consume`, { method: "POST",
        body: JSON.stringify({ qty: v.qty, consumed_on: v.consumed_on, chart_no: v.chart_no || null,
          client_ref: v.client_ref }) }),
    onSuccess: (r) => { toast.ok(`사용 기록 — 매입 ${won(r.amount)} (미지급 발생)`); setAct(null); refresh(); },
    onError: fail,
  });
  const ret = useMutation({
    mutationFn: (v: { id: number; qty: number; returned_on: string }) =>
      api(`/inventory/consignment/${v.id}/return`, { method: "POST",
        body: JSON.stringify({ qty: v.qty, returned_on: v.returned_on }) }),
    onSuccess: () => { toast.ok("반품 처리 완료"); setAct(null); refresh(); },
    onError: fail,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 위탁재고</h1>
        <Button onClick={() => setPlacing(true)}>위탁 적재</Button>
      </div>
      <QueryErrorBanner queries={[stock, usage]} />
      <p className="text-xs text-slate-500">
        고가 임플란트·스텐트를 공급사 소유 상태로 보관하다 사용 시점에 매입(소유권 이전·DR 재료비/
        CR 미지급금). 적재·반품은 회계 없음(미소유). 일반 소유재고(1150)와 분리.</p>

      <Card title="위탁 재고" action={
        <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="ACTIVE">사용가능(ACTIVE)</option>
          <option value="DEPLETED">소진(DEPLETED)</option>
          <option value="RETURNED">반품(RETURNED)</option>
        </select>}>
        <Table headers={["공급사", "품목", "로트", "유효기간", "적재", "잔량", "단가", "상태", ""]}
          rows={(stock.data?.items ?? []).map((s) => [
            supplierName(s.supplier_id), itemName(s.item_id), s.lot_no, s.expiry_on ?? "—",
            s.placed_qty, <b key="q">{s.qty_on_hand}</b>, won(s.unit_cost),
            <Badge key="st" tone={STATUS_TONE[s.status] ?? "gray"}>{s.status}</Badge>,
            s.status === "ACTIVE" ? (
              <span key="a" className="flex gap-1">
                <Button variant="secondary" size="sm" onClick={() => setAct({ stock: s, mode: "consume" })}>사용</Button>
                <Button variant="secondary" size="sm" onClick={() => setAct({ stock: s, mode: "return" })}>반품</Button>
              </span>) : "",
          ])} empty={stock.isFetching ? "불러오는 중…" : "해당 상태의 위탁재고 없음"} />
      </Card>

      <Card title="사용(매입) 내역" action={
        <div className="flex items-center gap-1.5">
          <input type="date" value={since} onChange={(e) => setSince(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs" />
          <span className="text-xs text-slate-400">~</span>
          <input type="date" value={until} onChange={(e) => setUntil(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs" />
          <span className="ml-2 text-xs font-semibold">합계 {won(usage.data?.total_amount ?? 0)}</span>
        </div>}>
        <Table headers={["사용일", "위탁ID", "수량", "매입액", "차트번호", "미지급"]}
          rows={(usage.data?.items ?? []).map((c) => [
            c.consumed_on, `#${c.consignment_id}`, c.qty, won(c.amount),
            c.chart_no ?? "—", c.payable_ref ?? "—",
          ])} empty={usage.isFetching ? "불러오는 중…" : "기간 내 사용 없음"} />
        {(usage.data?.items ?? []).some((c) => c.chart_masked) && (
          <p className="mt-2 text-xs text-slate-400">
            차트번호는 마스킹되어 표시됩니다 — 환자 청구 대사는 원무·재무 화면에서 수행하세요.
          </p>)}
      </Card>

      {placing && (
        <Modal title="위탁재고 적재" onClose={() => setPlacing(false)}>
          <PlaceForm suppliers={suppliers.data?.items ?? []} items={items.data?.items ?? []}
            isLoading={place.isPending} onSubmit={(b) => place.mutate(b)} />
        </Modal>
      )}
      {act && (
        <Modal title={`${act.mode === "consume" ? "위탁 사용(매입)" : "위탁 반품"} — ${itemName(act.stock.item_id)} (잔량 ${act.stock.qty_on_hand})`}
          onClose={() => setAct(null)}>
          {act.mode === "consume"
            ? <ConsumeForm stock={act.stock} isLoading={consume.isPending}
                onSubmit={(v) => consume.mutate({ ...v, id: act.stock.id })} />
            : <ReturnForm stock={act.stock} isLoading={ret.isPending}
                onSubmit={(v) => ret.mutate({ ...v, id: act.stock.id })} />}
        </Modal>
      )}
    </div>
  );
}

function PlaceForm({ suppliers, items, onSubmit, isLoading }: {
  suppliers: Supplier[]; items: Item[]; onSubmit: (b: Record<string, unknown>) => void; isLoading: boolean;
}) {
  const [supplierId, setSupplierId] = useState("");
  const [itemId, setItemId] = useState("");
  const [lotNo, setLotNo] = useState("");
  const [qty, setQty] = useState("");
  const [unitCost, setUnitCost] = useState("");
  const [placedOn, setPlacedOn] = useState(todayLocal());
  const [expiryOn, setExpiryOn] = useState("");
  const ok = supplierId && itemId && lotNo && Number(qty) > 0 && Number(unitCost) > 0;
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">공급사
        <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">— 선택 —</option>
          {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
        </select>
      </label>
      <label className="block text-sm text-slate-600">품목
        <select value={itemId} onChange={(e) => setItemId(e.target.value)}
          className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
          <option value="">— 선택 —</option>
          {items.map((i) => <option key={i.id} value={i.id}>{i.name} ({i.code})</option>)}
        </select>
      </label>
      <div className="grid grid-cols-2 gap-2">
        <Input label="로트번호" value={lotNo} onChange={(e) => setLotNo(e.target.value)} />
        <Input label="유효기간(선택)" type="date" value={expiryOn}
          onChange={(e) => setExpiryOn(e.target.value)} />
      </div>
      <div className="grid grid-cols-2 gap-2">
        <Input label="수량" value={qty} inputMode="numeric"
          onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
        <Input label="매입단가" value={unitCost} inputMode="numeric"
          onChange={(e) => setUnitCost(e.target.value.replace(/[^0-9]/g, ""))} />
      </div>
      <Input label="적재일" type="date" value={placedOn}
        onChange={(e) => setPlacedOn(e.target.value)} />
      <Button disabled={isLoading || !ok} onClick={() => onSubmit({
        supplier_id: Number(supplierId), item_id: Number(itemId), lot_no: lotNo,
        qty: Number(qty), unit_cost: Number(unitCost), placed_on: placedOn,
        expiry_on: expiryOn || null })}>적재</Button>
    </div>
  );
}

function ConsumeForm({ stock, onSubmit, isLoading }: {
  stock: Stock; onSubmit: (v: { qty: number; consumed_on: string; chart_no: string; client_ref: string }) => void;
  isLoading: boolean;
}) {
  const [qty, setQty] = useState("");
  const [consumedOn, setConsumedOn] = useState(todayLocal());
  const [chartNo, setChartNo] = useState("");
  const [ref] = useState(uuid());   // 멱등키 — 중복클릭/재전송 시 서버 디둡(중복 매입·미지급 방지)
  const n = Number(qty) || 0;
  const over = n > stock.qty_on_hand;
  return (
    <div className="space-y-2">
      <Input label={`사용 수량 (잔량 ${stock.qty_on_hand})`} value={qty} inputMode="numeric"
        onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
      {n > 0 && <p className="text-xs text-slate-500">
        매입 예상 {won(Math.round(n * stock.unit_cost))} (DR 재료비 / CR 미지급금)</p>}
      {over && <p className="text-xs text-red-600">잔량({stock.qty_on_hand})을 초과할 수 없습니다</p>}
      <Input label="사용일" type="date" value={consumedOn}
        onChange={(e) => setConsumedOn(e.target.value)} />
      <Input label="차트번호(선택)" value={chartNo} onChange={(e) => setChartNo(e.target.value)}
        placeholder="시술 환자 참조" />
      <Button disabled={isLoading || n <= 0 || over}
        onClick={() => onSubmit({ qty: n, consumed_on: consumedOn, chart_no: chartNo, client_ref: ref })}>
        사용 기록(매입)</Button>
    </div>
  );
}

function ReturnForm({ stock, onSubmit, isLoading }: {
  stock: Stock; onSubmit: (v: { qty: number; returned_on: string }) => void; isLoading: boolean;
}) {
  const [qty, setQty] = useState("");
  const [returnedOn, setReturnedOn] = useState(todayLocal());
  const n = Number(qty) || 0;
  const over = n > stock.qty_on_hand;
  return (
    <div className="space-y-2">
      <Input label={`반품 수량 (잔량 ${stock.qty_on_hand})`} value={qty} inputMode="numeric"
        onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
      {over && <p className="text-xs text-red-600">잔량({stock.qty_on_hand})을 초과할 수 없습니다</p>}
      <Input label="반품일" type="date" value={returnedOn}
        onChange={(e) => setReturnedOn(e.target.value)} />
      <p className="text-xs text-slate-500">공급사 반환 — 회계 처리 없음(미소유).</p>
      <Button disabled={isLoading || n <= 0 || over}
        onClick={() => onSubmit({ qty: n, returned_on: returnedOn })}>반품 처리</Button>
    </div>
  );
}
