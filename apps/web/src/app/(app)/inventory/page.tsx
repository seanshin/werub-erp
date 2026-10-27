"use client";
/** 재고 — 품목·입출고(FIFO)·마약류 수불부(정정 모달) (FR-11/12/46/20). */
import { todayLocal } from "@/lib/dates";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { AttachmentCell } from "@/components/attachment-cell";
import type { Item } from "@/types";
import { dt, won } from "@/lib/format";

interface LedgerRow { id: number; seq_no: number; entry_type: string; qty: number;
                      balance: number; handler_id: number; corrects_id: number | null; at: string }

const today = () => todayLocal();

export default function InventoryPage() {
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const [narcItem, setNarcItem] = useState<number | null>(null);
  const [correcting, setCorrecting] = useState<{ id: number } | null>(null);

  const items = useQuery({
    queryKey: ["items"],
    queryFn: () => api<{ items: Item[] }>("/inventory/items?size=100"),
  });
  const yyyymm = today().slice(0, 7);
  const ledger = useQuery({
    queryKey: ["narc-ledger", narcItem, yyyymm],
    queryFn: () => api<{ items: LedgerRow[] }>(
      `/inventory/narcotics/ledger?item_id=${narcItem}&yyyymm=${yyyymm}`),
    enabled: narcItem !== null,
  });

  const issue = useMutation({
    mutationFn: ({ item_id, qty }: { item_id: number; qty: number }) =>
      api("/inventory/movements/issue", { method: "POST",
        body: JSON.stringify({ item_id, qty, moved_on: today() }) }),
    onSuccess: () => { setMessage("출고 완료 (FIFO)"); qc.invalidateQueries({ queryKey: ["items"] }); },
    onError: (e) => setMessage(e instanceof ApiError ? `${e.error.code}: ${e.message}` : "출고 실패"),
  });

  const narcEntry = useMutation({
    mutationFn: (body: { item_id: number; entry_type: string; qty: number }) =>
      api("/inventory/narcotics/entries", { method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => { setMessage("수불 기록 완료"); qc.invalidateQueries({ queryKey: ["narc-ledger"] }); },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "기록 실패"),
  });

  const pos = useQuery({
    queryKey: ["pos"],
    queryFn: () => api<{ items: { id: number; supplier_id: number; status: string;
      requested_at: string; total_amount: number;
      // 미입고 잔량 = 3-way 대사의 핵심. 분할입고(PARTIAL) 진행률을 화면에서 못 보던 값.
      lines: { item_id: number; qty: number; unit_price: number;
        received_qty: number; outstanding_qty: number }[] }[] }>(
      "/inventory/purchase-orders"),
  });
  const approvePo = useMutation({
    mutationFn: (id: number) => api(`/inventory/purchase-orders/${id}/approve`, { method: "PATCH" }),
    onSuccess: () => { setMessage("발주 승인"); qc.invalidateQueries({ queryKey: ["pos"] }); },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "승인 실패"),  // 경고-5
  });
  const receiveStock = useMutation({
    mutationFn: (body: object) => api("/inventory/movements/receive", {
      method: "POST", body: JSON.stringify(body) }),
    onSuccess: () => { setMessage("입고 완료"); qc.invalidateQueries({ queryKey: ["items"] }); },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "입고 실패"),
  });

  const correct = useMutation({
    mutationFn: ({ id, qty, reason }: { id: number; qty: number; reason: string }) =>
      api(`/inventory/narcotics/entries/${id}/correct`, {
        method: "POST", body: JSON.stringify({ corrected_qty: qty, reason }) }),
    onSuccess: () => {
      setMessage("정정 레코드 추가됨 (원본 보존 — append-only)");
      setCorrecting(null);
      qc.invalidateQueries({ queryKey: ["narc-ledger"] });
    },
    onError: (e) => setMessage(e instanceof ApiError ? e.message : "정정 실패"),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-lg font-bold">재고 — 품목·마약류</h1>
      {message && <p className="text-sm text-blue-600">{message}</p>}

      <Card title="입고 (로트·유효기간 필수)">
        <ReceiveForm pending={receiveStock.isPending} onSubmit={(b) => receiveStock.mutate(b)} />
      </Card>

      <Card title="발주 보드 (요청→승인→입고)">
        <Table headers={["#", "공급처", "상태", "발주총액", "라인 (입고/발주)", "요청일", "증빙", ""]}
          rows={(pos.data?.items ?? []).map((p) => [
            p.id, p.supplier_id,
            <Badge key="s" tone={p.status === "RECEIVED" ? "green"
              : p.status === "APPROVED" ? "blue" : "amber"}>{p.status}</Badge>,
            // 승인 버튼이 같은 행에 있는데 금액이 없어 판단 근거 없이 눌러야 했다
            won(p.total_amount),
            <span key="l" className="text-xs">
              {p.lines.map((l) => (
                <span key={l.item_id} className="mr-2 whitespace-nowrap">
                  #{l.item_id} {l.received_qty}/{l.qty}
                  {l.outstanding_qty > 0 && (
                    <span className="ml-0.5 text-amber-600">(미입고 {l.outstanding_qty})</span>)}
                </span>
              ))}
            </span>,
            dt(p.requested_at).slice(0, 10),
            <AttachmentCell key="att" entityType="po_receipt" entityId={p.id} />,
            p.status === "REQUESTED" &&
              <Button key="a" variant="secondary" disabled={approvePo.isPending} onClick={() => approvePo.mutate(p.id)}>승인</Button>,
          ])} empty="발주 없음" />
      </Card>

      <Card title="품목·재고 현황">
        <Table headers={["코드", "품명", "재고", "안전재고", "마약류", "출고(FIFO)"]}
          rows={(items.data?.items ?? []).map((it) => [
            it.code, it.name,
            <span key="s" className={it.stock < it.safety_stock ? "font-bold text-red-600" : ""}>
              {it.stock}</span>,
            it.safety_stock,
            it.is_narcotic
              ? <button key="n" className="text-red-600 underline" onClick={() => setNarcItem(it.id)}>
                  수불부</button>
              : "-",
            <IssueCell key="i" pending={issue.isPending} onIssue={(qty) => issue.mutate({ item_id: it.id, qty })} />,
          ])} />
        <p className="mt-2 text-xs text-slate-400">
          유효기간 3개월 미만 lot 출고는 자동 차단됩니다 (관리자 override는 사유 필수)
        </p>
      </Card>

      {narcItem !== null && (
        <Card title={`마약류 수불부 — ${yyyymm} (append-only · 해시 체인)`}
          action={<NarcEntryForm pending={narcEntry.isPending} onSubmit={(t, q) =>
            narcEntry.mutate({ item_id: narcItem, entry_type: t, qty: q })} />}>
          <Table headers={["순번", "구분", "수량", "잔량", "취급자", "원본참조", "일시", ""]}
            rows={(ledger.data?.items ?? []).map((r) => [
              r.seq_no,
              <Badge key="t" tone={r.entry_type === "CORRECTION" ? "amber"
                : r.entry_type === "IN" ? "green" : "gray"}>{r.entry_type}</Badge>,
              r.qty, r.balance, r.handler_id, r.corrects_id ?? "-",
              dt(r.at),
              r.entry_type !== "CORRECTION" &&
                <Button key="c" variant="secondary"
                  onClick={() => setCorrecting({ id: r.id })}>정정</Button>,
            ])} />
        </Card>
      )}

      {correcting && (
        <Modal title="수불 정정 (원본 보존 — 차액만 기록, 사유 필수)"
          onClose={() => setCorrecting(null)}>
          <CorrectForm pending={correct.isPending} onSubmit={(qty, reason) =>
            correct.mutate({ id: correcting.id, qty, reason })} />
        </Modal>
      )}
    </div>
  );
}

function ReceiveForm({ onSubmit, pending }: { onSubmit: (b: object) => void; pending?: boolean }) {
  const [f, setF] = useState({ item_id: "", lot_no: "", expiry_on: "", qty: "" });
  const set = (k: string) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setF({ ...f, [k]: e.target.value });
  return (
    <div className="flex flex-wrap items-end gap-2">
      <Input label="품목 ID" value={f.item_id} onChange={set("item_id")} />
      <Input label="로트번호" value={f.lot_no} onChange={set("lot_no")} />
      <Input label="유효기간" type="date" value={f.expiry_on} onChange={set("expiry_on")} />
      <Input label="수량" value={f.qty} onChange={set("qty")} />
      <Button variant="secondary"
        disabled={pending}
        onClick={() => f.item_id && f.lot_no && f.expiry_on && f.qty &&
          onSubmit({ item_id: Number(f.item_id), lot_no: f.lot_no, expiry_on: f.expiry_on,
                     qty: Number(f.qty), moved_on: today() })}>입고</Button>
    </div>
  );
}

function IssueCell({ onIssue, pending }: { onIssue: (qty: number) => void; pending?: boolean }) {
  const [qty, setQty] = useState("");
  return (
    <span className="flex items-center gap-1">
      <input value={qty} onChange={(e) => setQty(e.target.value)} placeholder="수량"
        className="w-16 rounded border border-slate-300 px-1 py-0.5 text-xs" />
      <Button variant="secondary" disabled={!qty || pending} onClick={() => qty && onIssue(Number(qty))}>출고</Button>
    </span>
  );
}

function NarcEntryForm({ onSubmit, pending }: { onSubmit: (type: string, qty: number) => void; pending?: boolean }) {
  const [type, setType] = useState("IN");
  const [qty, setQty] = useState("");
  return (
    <span className="flex items-center gap-1">
      <select value={type} onChange={(e) => setType(e.target.value)}
        className="rounded border border-slate-300 px-1 py-0.5 text-xs">
        {["IN", "OUT", "DISPOSE", "ADJUST"].map((t) => <option key={t}>{t}</option>)}
      </select>
      <input value={qty} onChange={(e) => setQty(e.target.value)} placeholder="수량"
        className="w-16 rounded border border-slate-300 px-1 py-0.5 text-xs" />
      <Button variant="secondary" disabled={!qty || pending} onClick={() => qty && onSubmit(type, Number(qty))}>기록</Button>
    </span>
  );
}

function CorrectForm({ onSubmit, pending }: { onSubmit: (qty: number, reason: string) => void; pending?: boolean }) {
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  return (
    <div className="space-y-2">
      <Input label="실제 수량" autoFocus value={qty} onChange={(e) => setQty(e.target.value)} />
      <Input label="정정 사유 (필수)" value={reason} onChange={(e) => setReason(e.target.value)} />
      <Button disabled={!qty || !reason || pending} onClick={() => qty && reason && onSubmit(Number(qty), reason)}>정정 기록</Button>
    </div>
  );
}
