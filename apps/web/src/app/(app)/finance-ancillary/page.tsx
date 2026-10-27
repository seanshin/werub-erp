"use client";
/** 재무 — 부대사업 수익(의료외수익): 항목 마스터 관리 + 수납 기록(과세=순액/매출세액 분리) + 기간 집계. */
import { todayLocal } from "@/lib/dates";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryErrorBanner } from "@/components/query-guard";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { won } from "@/lib/format";
import { useToast } from "@/components/toast";

const CATEGORIES: Record<string, string> = {
  PARKING: "주차", RENTAL: "임대", CAFETERIA: "식당", CHECKUP: "비보험검진",
  FUNERAL: "장례식장", OTHER: "기타",
};
const METHODS = ["CASH", "CARD", "TRANSFER"];

interface Item {
  id: number; name: string; category: string; taxable: boolean; account_code: string;
  default_price: number | null; active: boolean; note: string | null;
}
interface Summary {
  since: string; until: string; total_gross: number; total_net: number; total_vat: number;
  by_category: { category: string; gross: number; net: number; vat: number; count: number }[];
}
type ItemDraft = { name: string; category: string; taxable: boolean; default_price: string; note: string };
const BLANK_ITEM: ItemDraft = { name: "", category: "PARKING", taxable: true, default_price: "", note: "" };

function firstOfMonth(): string { return todayLocal().slice(0, 8) + "01"; }
function uuid(): string {
  return typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : String(Date.now());
}

export default function AncillaryRevenuePage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [newItem, setNewItem] = useState(false);
  const [recordFor, setRecordFor] = useState<Item | null>(null);
  const [since, setSince] = useState(firstOfMonth());
  const [until, setUntil] = useState(todayLocal());

  const items = useQuery({ queryKey: ["ancillary-items"], queryFn: () =>
    api<{ items: Item[] }>("/finance/ancillary/items") });
  const summary = useQuery({ queryKey: ["ancillary-summary", since, until], queryFn: () =>
    api<Summary>(`/finance/ancillary/summary?since=${since}&until=${until}`) });
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["ancillary-items"] });
    qc.invalidateQueries({ queryKey: ["ancillary-summary"] });
  };

  const createItem = useMutation({
    mutationFn: (d: ItemDraft) => api("/finance/ancillary/items", { method: "POST",
      body: JSON.stringify({ name: d.name, category: d.category, taxable: d.taxable,
        default_price: d.default_price ? Number(d.default_price) : null }) }),
    onSuccess: () => { toast.ok("항목 추가 완료"); setNewItem(false); refresh(); },
    onError: fail,
  });
  const toggleItem = useMutation({
    mutationFn: (it: Item) => api(`/finance/ancillary/items/${it.id}?active=${!it.active}`,
      { method: "PATCH" }),
    onSuccess: () => refresh(),
    onError: fail,
  });
  const record = useMutation({
    mutationFn: (v: { item_id: number; amount: number; method: string; occurred_on: string;
      qty: number; note: string; client_ref: string }) =>
      api<{ id: number; net_amount: number; vat_amount: number }>("/finance/ancillary/revenues",
        { method: "POST", body: JSON.stringify(v) }),
    onSuccess: (r) => { toast.ok(`수납 완료 — 순매출 ${won(r.net_amount)} / 부가세 ${won(r.vat_amount)}`);
      setRecordFor(null); refresh(); },
    onError: fail,
  });

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재무 — 부대사업 수익</h1>
        <Button onClick={() => setNewItem(true)}>항목 추가</Button>
      </div>
      <QueryErrorBanner queries={[items, summary]} />
      <p className="text-xs text-slate-500">
        주차·임대·비보험검진 등 의료외수익(4130). 과세 항목은 수납액을 순매출(4130)+매출세액(2135)으로
        분리 전기합니다(면세 의료용역과 구분).</p>

      <Card title="기간 집계" action={
        <div className="flex items-center gap-1.5">
          <input type="date" value={since} onChange={(e) => setSince(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs" />
          <span className="text-xs text-slate-400">~</span>
          <input type="date" value={until} onChange={(e) => setUntil(e.target.value)}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs" />
        </div>}>
        <div className="mb-3 grid gap-3 sm:grid-cols-3">
          <div><p className="text-xs text-slate-500">총 수납</p><p className="text-xl font-bold">{won(summary.data?.total_gross ?? 0)}</p></div>
          <div><p className="text-xs text-slate-500">순매출(4130)</p><p className="text-xl font-bold text-emerald-700">{won(summary.data?.total_net ?? 0)}</p></div>
          <div><p className="text-xs text-slate-500">매출세액(2135)</p><p className="text-xl font-bold text-slate-600">{won(summary.data?.total_vat ?? 0)}</p></div>
        </div>
        <Table headers={["분류", "건수", "총 수납", "순매출", "매출세액"]}
          rows={(summary.data?.by_category ?? []).map((c) => [
            CATEGORIES[c.category] ?? c.category, `${c.count}건`,
            won(c.gross), won(c.net), won(c.vat),
          ])} empty={summary.isFetching ? "불러오는 중…" : "기간 내 수납 없음"} />
      </Card>

      <Card title={`항목 마스터 (${items.data?.items.length ?? 0})`}>
        <Table headers={["항목", "분류", "과세", "기본가", "상태", ""]}
          rows={(items.data?.items ?? []).map((it) => [
            it.name,
            <Badge key="c" tone="blue">{CATEGORIES[it.category] ?? it.category}</Badge>,
            it.taxable ? <Badge key="t" tone="amber">과세</Badge> : <Badge key="t" tone="gray">면세</Badge>,
            it.default_price != null ? won(it.default_price) : "—",
            it.active ? <Badge key="a" tone="green">활성</Badge> : <Badge key="a" tone="gray">비활성</Badge>,
            <span key="e" className="flex gap-1">
              <Button variant="secondary" size="sm" disabled={!it.active}
                onClick={() => setRecordFor(it)}>수납</Button>
              <Button variant="secondary" size="sm" isLoading={toggleItem.isPending}
                onClick={() => toggleItem.mutate(it)}>{it.active ? "비활성" : "활성"}</Button>
            </span>,
          ])} empty="등록된 항목 없음" />
      </Card>

      {newItem && (
        <Modal title="부대사업 항목 추가" onClose={() => setNewItem(false)}>
          <ItemForm isLoading={createItem.isPending} onSubmit={(d) => createItem.mutate(d)} />
        </Modal>
      )}
      {recordFor && (
        <Modal title={`수납 — ${recordFor.name}`} onClose={() => setRecordFor(null)}>
          <RecordForm item={recordFor} isLoading={record.isPending}
            onSubmit={(v) => record.mutate({ ...v, item_id: recordFor.id })} />
        </Modal>
      )}
    </div>
  );
}

function ItemForm({ onSubmit, isLoading }: { onSubmit: (d: ItemDraft) => void; isLoading: boolean }) {
  const [f, setF] = useState<ItemDraft>(BLANK_ITEM);
  const set = (p: Partial<ItemDraft>) => setF({ ...f, ...p });
  return (
    <div className="space-y-2">
      <Input label="항목명" value={f.name} onChange={(e) => set({ name: e.target.value })}
        placeholder="예: 외래 주차" />
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">분류
          <select value={f.category} onChange={(e) => set({ category: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(CATEGORIES).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="기본가(선택)" value={f.default_price} inputMode="numeric"
          onChange={(e) => set({ default_price: e.target.value.replace(/[^0-9]/g, "") })} />
      </div>
      <label className="flex items-center gap-1.5 text-sm text-slate-600">
        <input type="checkbox" checked={f.taxable}
          onChange={(e) => set({ taxable: e.target.checked })} /> 과세(부가세 10% 분리)</label>
      <Button disabled={isLoading || !f.name} onClick={() => onSubmit(f)}>추가</Button>
    </div>
  );
}

function RecordForm({ item, onSubmit, isLoading }: {
  item: Item; onSubmit: (v: { amount: number; method: string; occurred_on: string;
    qty: number; note: string; client_ref: string }) => void; isLoading: boolean;
}) {
  const [amount, setAmount] = useState(item.default_price != null ? String(item.default_price) : "");
  const [method, setMethod] = useState("CASH");
  const [occurredOn, setOccurredOn] = useState(todayLocal());
  const [qty, setQty] = useState("1");
  const [note, setNote] = useState("");
  const [ref] = useState(uuid());   // 멱등키 — 재전송/중복 클릭 시 서버 디둡(중복 수익/전표 방지)
  const amt = Number(amount) || 0;
  const net = item.taxable ? Math.round(amt / 1.1) : amt;
  return (
    <div className="space-y-2">
      <Input label="수납액(부가세 포함)" value={amount} inputMode="numeric"
        onChange={(e) => setAmount(e.target.value.replace(/[^0-9]/g, ""))} />
      {item.taxable && amt > 0 && (
        <p className="text-xs text-slate-500">순매출 {won(net)} + 매출세액 {won(amt - net)}</p>)}
      <div className="grid grid-cols-2 gap-2">
        <label className="block text-sm text-slate-600">수납방법
          <select value={method} onChange={(e) => setMethod(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <Input label="수량" value={qty} inputMode="numeric"
          onChange={(e) => setQty(e.target.value.replace(/[^0-9]/g, ""))} />
      </div>
      <Input label="수납일" type="date" value={occurredOn}
        onChange={(e) => setOccurredOn(e.target.value)} />
      <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
      <Button disabled={isLoading || amt <= 0}
        onClick={() => onSubmit({ amount: amt, method, occurred_on: occurredOn,
          qty: Number(qty) || 1, note, client_ref: ref })}>수납 기록</Button>
    </div>
  );
}
