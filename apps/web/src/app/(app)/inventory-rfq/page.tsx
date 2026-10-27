"use client";
/** 재고 — 소싱(RFQ→견적→낙찰→단가계약). 견적요청 등록→개찰→벤더 견적 접수→총액 비교→낙찰
 *  (단가계약 자동 생성·나머지 반려). PO가 단가계약 단가를 자동 적용. scm.po write=ADMIN/부서장. */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal } from "@/lib/dates";
import { useToast } from "@/components/toast";

const RFQ_STATUS: Record<string, string> = { DRAFT: "작성중", OPEN: "개찰(견적접수)",
  CLOSED: "마감", AWARDED: "낙찰완료", CANCELED: "취소" };
const stTone = (s: string) => s === "AWARDED" ? "green" : s === "OPEN" ? "blue"
  : s === "CANCELED" ? "red" : "gray";

interface Rfq {
  id: number; title: string; category: string | null; due_on: string | null;
  status: string; awarded_quote_id: number | null; line_count: number; quote_count: number;
}
interface RfqLine { item_id: number; qty: number; spec: string | null }
// 총액만 보면 **최저가만 보이고 납기가 안 보여** "싸지만 3개월 걸리는" 견적을 낙찰한다.
// 품목별 단가도 없어 총액 근소차 견적의 우열을 가릴 수 없었다.
interface RfqQuote { quote_id: number; supplier_id: number; total: number; status: string;
  max_lead_time_days: number | null;
  lines: { item_id: number; unit_price: number; lead_time_days: number | null }[] }
interface RfqDetail {
  id: number; title: string; category: string | null; due_on: string | null; status: string;
  note: string | null; awarded_quote_id: number | null; lines: RfqLine[]; quotes: RfqQuote[];
}
interface Item { id: number; code: string; name: string }
interface Supplier { id: number; name: string }
// PO가 이 단가를 자동 적용하는데(po_service) 계약단가를 확인할 경로가 어디에도 없었다.
interface PA { id: number; supplier_id: number; title: string; valid_from: string; valid_to: string;
  lines: { item_id: number; unit_price: number }[] }

export default function RfqPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [selected, setSelected] = useState<number | null>(null);
  const [creating, setCreating] = useState(false);
  const [quoting, setQuoting] = useState(false);
  const [awarding, setAwarding] = useState<RfqQuote | null>(null);

  const rfqs = useQuery({ queryKey: ["rfqs"], queryFn: () =>
    api<{ items: Rfq[] }>("/inventory/rfqs") });
  const detail = useQuery({ queryKey: ["rfq", selected], enabled: selected != null, queryFn: () =>
    api<RfqDetail>(`/inventory/rfqs/${selected}`) });
  const items = useQuery({ queryKey: ["scm-items"], queryFn: () =>
    api<{ items: Item[] }>("/inventory/items?size=1000") });
  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: () =>
    api<{ items: Supplier[] }>("/inventory/suppliers") });
  const pas = useQuery({ queryKey: ["price-agreements"], queryFn: () =>
    api<{ items: PA[] }>("/inventory/price-agreements") });

  const itemName = useMemo(() => Object.fromEntries(
    (items.data?.items ?? []).map((i) => [i.id, `${i.code} ${i.name}`])), [items.data]);
  const supName = useMemo(() => Object.fromEntries(
    (suppliers.data?.items ?? []).map((s) => [s.id, s.name])), [suppliers.data]);

  const refresh = () => { qc.invalidateQueries({ queryKey: ["rfqs"] });
    if (selected != null) qc.invalidateQueries({ queryKey: ["rfq", selected] });
    qc.invalidateQueries({ queryKey: ["price-agreements"] }); };

  const openRfq = useMutation({
    mutationFn: (id: number) => api(`/inventory/rfqs/${id}/open`, { method: "POST" }),
    onSuccess: () => { toast.ok("개찰 — 견적 접수 시작"); refresh(); }, onError: fail });

  const d = detail.data;
  const bestTotal = d?.quotes.length ? Math.min(...d.quotes.map((q) => q.total)) : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 소싱(RFQ·견적·단가계약)</h1>
        <Button onClick={() => setCreating(true)}>견적요청(RFQ) 생성</Button>
      </div>
      <p className="text-xs text-slate-500">
        견적요청 등록 → 개찰 → 벤더 견적 접수 → 총액 비교 → 낙찰(단가계약 자동 생성). PO가 단가계약
        단가를 자동 적용합니다. 직무분리·승인은 PO/계약 결재선.</p>

      <QueryErrorBanner queries={[rfqs, pas]} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="RFQ 목록">
          <Table headers={["제목", "라인", "견적", "마감", "상태"]}
            rows={(rfqs.data?.items ?? []).map((r) => [
              <button key="t" className="text-left text-blue-600 hover:underline"
                onClick={() => setSelected(r.id)}>{r.title}</button>,
              `${r.line_count}품목`, `${r.quote_count}건`, r.due_on ?? "—",
              <Badge key="s" tone={stTone(r.status)}>{RFQ_STATUS[r.status] ?? r.status}</Badge>,
            ])} empty={rfqs.isFetching ? "불러오는 중…" : "RFQ 없음"} />
        </Card>

        <Card title={d ? `RFQ 상세 — ${d.title}` : "RFQ 상세"}>
          {!d ? <p className="text-sm text-slate-400">좌측에서 RFQ를 선택하세요.</p> : (
            <div className="space-y-3">
              <div className="flex items-center gap-2">
                <Badge tone={stTone(d.status)}>{RFQ_STATUS[d.status] ?? d.status}</Badge>
                {d.status === "DRAFT" && <Button size="sm" disabled={openRfq.isPending}
                  onClick={() => openRfq.mutate(d.id)}>개찰</Button>}
                {(d.status === "OPEN" || d.status === "CLOSED") && (
                  <Button size="sm" variant="secondary" onClick={() => setQuoting(true)}>견적 접수</Button>)}
              </div>
              <div>
                <p className="mb-1 text-xs font-semibold text-slate-500">요청 품목</p>
                <Table headers={["품목", "수량", "규격"]}
                  rows={d.lines.map((ln) => [itemName[ln.item_id] ?? `#${ln.item_id}`, ln.qty, ln.spec ?? "—"])}
                  empty="—" />
              </div>
              <div>
                <p className="mb-1 text-xs font-semibold text-slate-500">접수 견적 (총액 오름차순)</p>
                <Table headers={["공급사", "총액", "최장 납기", "품목별 단가", "상태", ""]}
                  rows={d.quotes.map((q) => [
                    supName[q.supplier_id] ?? `#${q.supplier_id}`,
                    <span key="t" className={q.total === bestTotal ? "font-bold text-green-700" : ""}>
                      {won(q.total)}{q.total === bestTotal ? " (최저)" : ""}</span>,
                    // 최저가 낙찰이 곧 최선이 아니다 — 납기가 판단을 바꾼다
                    q.max_lead_time_days != null
                      ? <span key="lt" className={q.max_lead_time_days >= 30 ? "text-amber-600" : ""}>
                          {q.max_lead_time_days}일</span>
                      : "—",
                    <span key="ul" className="text-[11px] text-slate-500">
                      {q.lines.map((l) => `#${l.item_id} ${won(l.unit_price)}`).join(" · ") || "—"}
                    </span>,
                    <Badge key="s" tone={q.status === "AWARDED" ? "green" : q.status === "REJECTED" ? "gray" : "blue"}>
                      {q.status === "AWARDED" ? "낙찰" : q.status === "REJECTED" ? "반려" : "접수"}</Badge>,
                    (d.status === "OPEN" || d.status === "CLOSED") && q.status === "SUBMITTED"
                      ? <Button key="a" size="sm" onClick={() => setAwarding(q)}>낙찰</Button> : "",
                  ])} empty="견적 없음" />
              </div>
            </div>
          )}
        </Card>
      </div>

      <Card title="유효 단가계약">
        <Table headers={["계약명", "공급사", "시작", "종료", "품목별 계약단가"]}
          rows={(pas.data?.items ?? []).map((p) => [
            p.title, supName[p.supplier_id] ?? `#${p.supplier_id}`, p.valid_from, p.valid_to,
            // PO가 자동 적용하는 단가 — 이 값이 안 보이면 발주 금액을 검증할 수 없다
            <span key="pl" className="text-[11px] text-slate-500">
              {p.lines.map((l) => `#${l.item_id} ${won(l.unit_price)}`).join(" · ") || "—"}
            </span>,
          ])}
          empty={pas.isFetching ? "불러오는 중…" : "단가계약 없음"} />
      </Card>

      {creating && <CreateModal items={items.data?.items ?? []}
        onDone={(id) => { setCreating(false); refresh(); setSelected(id); }} onError={fail}
        onClose={() => setCreating(false)} />}
      {quoting && d && <QuoteModal rfq={d} suppliers={suppliers.data?.items ?? []}
        itemName={itemName} onDone={() => { setQuoting(false); refresh(); }} onError={fail}
        onClose={() => setQuoting(false)} />}
      {awarding && d && <AwardModal rfqId={d.id} quote={awarding} supName={supName}
        onDone={() => { setAwarding(null); refresh(); }} onError={fail}
        onClose={() => setAwarding(null)} />}
    </div>
  );
}

function CreateModal({ items, onDone, onError, onClose }: {
  items: Item[]; onDone: (id: number) => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [title, setTitle] = useState("");
  const [dueOn, setDueOn] = useState("");
  const [lines, setLines] = useState<{ item_id: string; qty: string; spec: string }[]>(
    [{ item_id: "", qty: "", spec: "" }]);
  const setLine = (i: number, patch: Partial<{ item_id: string; qty: string; spec: string }>) =>
    setLines((ls) => ls.map((l, j) => j === i ? { ...l, ...patch } : l));
  const valid = title.trim() && lines.some((l) => l.item_id && Number(l.qty) > 0);
  const m = useMutation({
    mutationFn: () => api<{ id: number }>("/inventory/rfqs", { method: "POST", body: JSON.stringify({
      title, due_on: dueOn || null,
      lines: lines.filter((l) => l.item_id && Number(l.qty) > 0).map((l) => ({
        item_id: Number(l.item_id), qty: Number(l.qty), spec: l.spec || null })) }) }),
    onSuccess: (r) => { toast.ok("RFQ 생성"); onDone(r.id); }, onError });
  return (
    <Modal title="견적요청(RFQ) 생성" dirty size="lg" onClose={onClose}>
      <div className="space-y-2">
        <Input label="제목" value={title} onChange={(e) => setTitle(e.target.value)} />
        <Input label="견적 마감일(선택)" type="date" value={dueOn}
          onChange={(e) => setDueOn(e.target.value)} />
        <p className="text-xs font-semibold text-slate-500">요청 품목</p>
        {lines.map((l, i) => (
          <div key={i} className="flex gap-1">
            <select value={l.item_id} onChange={(e) => setLine(i, { item_id: e.target.value })}
              className="flex-1 rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">품목…</option>
              {items.map((it) => <option key={it.id} value={it.id}>{it.code} {it.name}</option>)}
            </select>
            <input placeholder="수량" value={l.qty} inputMode="decimal"
              onChange={(e) => setLine(i, { qty: e.target.value.replace(/[^0-9.]/g, "") })}
              className="w-20 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            <input placeholder="규격" value={l.spec}
              onChange={(e) => setLine(i, { spec: e.target.value })}
              className="w-24 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
            {lines.length > 1 && <button onClick={() => setLines((ls) => ls.filter((_, j) => j !== i))}
              className="px-1 text-slate-400 hover:text-red-500">✕</button>}
          </div>))}
        <Button size="sm" variant="secondary"
          onClick={() => setLines((ls) => [...ls, { item_id: "", qty: "", spec: "" }])}>+ 품목 추가</Button>
        <Button disabled={m.isPending || !valid} onClick={() => m.mutate()}>RFQ 생성</Button>
      </div>
    </Modal>
  );
}

function QuoteModal({ rfq, suppliers, itemName, onDone, onError, onClose }: {
  rfq: RfqDetail; suppliers: Supplier[]; itemName: Record<number, string>;
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [supplierId, setSupplierId] = useState("");
  const [prices, setPrices] = useState<Record<number, string>>({});
  const valid = supplierId && rfq.lines.every((ln) => Number(prices[ln.item_id]) > 0);
  const m = useMutation({
    mutationFn: () => api(`/inventory/rfqs/${rfq.id}/quotes`, { method: "POST", body: JSON.stringify({
      supplier_id: Number(supplierId), submitted_on: todayLocal(),
      lines: rfq.lines.map((ln) => ({ item_id: ln.item_id, unit_price: Number(prices[ln.item_id]) })) }) }),
    onSuccess: () => { toast.ok("견적 접수"); onDone(); }, onError });
  return (
    <Modal title="벤더 견적 접수" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">공급사
          <select value={supplierId} onChange={(e) => setSupplierId(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">선택…</option>
            {suppliers.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
          </select>
        </label>
        <p className="text-xs font-semibold text-slate-500">품목별 단가</p>
        {rfq.lines.map((ln) => (
          <div key={ln.item_id} className="flex items-center gap-2">
            <span className="flex-1 text-sm">{itemName[ln.item_id] ?? `#${ln.item_id}`}
              <span className="text-xs text-slate-400"> ×{ln.qty}</span></span>
            <input placeholder="단가" value={prices[ln.item_id] ?? ""} inputMode="decimal"
              onChange={(e) => setPrices((p) => ({ ...p,
                [ln.item_id]: e.target.value.replace(/[^0-9.]/g, "") }))}
              className="w-28 rounded-md border border-slate-300 px-2 py-1.5 text-sm" />
          </div>))}
        <Button disabled={m.isPending || !valid} onClick={() => m.mutate()}>견적 접수</Button>
      </div>
    </Modal>
  );
}

function AwardModal({ rfqId, quote, supName, onDone, onError, onClose }: {
  rfqId: number; quote: RfqQuote; supName: Record<number, string>;
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [validFrom, setValidFrom] = useState(todayLocal());
  const [validTo, setValidTo] = useState("");
  const m = useMutation({
    mutationFn: () => api(`/inventory/rfqs/${rfqId}/award`, { method: "POST", body: JSON.stringify({
      quote_id: quote.quote_id, valid_from: validFrom, valid_to: validTo }) }),
    onSuccess: () => { toast.ok("낙찰 — 단가계약 생성"); onDone(); }, onError });
  return (
    <Modal title="낙찰(단가계약 생성)" dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-sm text-slate-600">
          {supName[quote.supplier_id] ?? `#${quote.supplier_id}`} 견적(총액 {won(quote.total)})을
          낙찰합니다. 나머지 견적은 자동 반려됩니다.</p>
        <Input label="단가계약 시작일" type="date" value={validFrom}
          onChange={(e) => setValidFrom(e.target.value)} />
        <Input label="단가계약 종료일" type="date" value={validTo}
          onChange={(e) => setValidTo(e.target.value)} />
        <Button disabled={m.isPending || !validTo || validTo <= validFrom} onClick={() => m.mutate()}>
          낙찰 확정</Button>
      </div>
    </Modal>
  );
}
