"use client";
/** 재고 — 자재청구(불출요청·0217): 병동/부서 청구 → 승인(SoD·부서경계) → FIFO 출고.
 *  승인/불출은 서버 RBAC(scm.requisition approve)이 게이트 — 무승인 즉시출고 갭 보완 흐름. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { Combobox } from "@/components/combobox";
import { todayLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red" | "blue"> = {
  REQUESTED: "amber", APPROVED: "green", ISSUED: "blue", REJECTED: "red", CANCELED: "gray",
};
const APPROVER_ROLES = new Set(["DIRECTOR", "ADMIN", "MANAGER", "LOGISTICS_MANAGER"]);

interface MReq {
  id: number; req_no: string; department_id: number | null; requester: number;
  item_id: number; qty: number; reason: string | null; status: string; issued_on: string | null;
}
interface Item { id: number; code: string; name: string; stock: number | null; safety_stock: number }  // stock=null: 마약류(권한 없음)

export default function MaterialRequestsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const myId = Number(auth.claims()?.sub ?? 0);
  const canApprove = APPROVER_ROLES.has(auth.claims()?.role ?? "");

  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const [issuing, setIssuing] = useState<MReq | null>(null);

  const reqs = useQuery({ queryKey: ["mreqs", status], queryFn: () =>
    api<{ items: MReq[] }>(`/inventory/material-requests${status ? `?status=${status}` : ""}`) });
  const items = useQuery({ queryKey: ["items-all"], queryFn: () =>
    api<{ items: Item[] }>("/inventory/requestable-items") });
  const itemName = (id: number) => {
    const it = items.data?.items.find((x) => x.id === id);
    return it ? `${it.name} (${it.code})` : `품목#${id}`;
  };
  const refresh = () => qc.invalidateQueries({ queryKey: ["mreqs"] });

  const decide = useMutation({
    mutationFn: (v: { id: number; approve: boolean }) =>
      api(`/inventory/material-requests/${v.id}/decide`, { method: "POST",
        body: JSON.stringify({ approve: v.approve, reason: "" }) }),
    onSuccess: (_r, v) => { toast.ok(v.approve ? "승인" : "반려"); refresh(); },
    onError: fail,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => api(`/inventory/material-requests/${id}/cancel`, { method: "POST" }),
    onSuccess: () => { toast.ok("청구 회수"); refresh(); }, onError: fail,
  });

  const onDecide = async (r: MReq, approve: boolean) => {
    if (await confirm({ title: approve ? "자재청구 승인" : "자재청구 반려",
      body: `${r.req_no} (${itemName(r.item_id)} × ${r.qty})을(를) ${approve ? "승인" : "반려"}할까요?`,
      danger: !approve, confirmLabel: approve ? "승인" : "반려" })) decide.mutate({ id: r.id, approve });
  };
  const onCancel = async (r: MReq) => {
    if (await confirm({ title: "청구 회수", body: `${r.req_no}을(를) 회수할까요?`,
      confirmLabel: "회수" })) cancel.mutate(r.id);
  };

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 자재청구(불출요청)</h1>
        <Button onClick={() => setCreating(true)}>자재청구</Button>
      </div>
      <p className="text-xs text-slate-500">
        병동/부서가 중앙창고 불출을 청구하면 승인(직무분리 — 청구자 본인 승인 불가·부서장은 본부서만)
        후에만 FIFO 출고됩니다. 무승인 즉시 출고는 차단됩니다.</p>

      <QueryErrorBanner queries={[reqs]} />
      <Card title="자재청구" action={
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체</option>
          {["REQUESTED", "APPROVED", "ISSUED", "REJECTED", "CANCELED"].map((s) =>
            <option key={s} value={s}>{s}</option>)}
        </select>}>
        <Table headers={["청구번호", "부서", "품목", "수량", "사유", "상태", "불출일", ""]}
          rows={(reqs.data?.items ?? []).map((r) => [
            r.req_no, r.department_id != null ? `부서#${r.department_id}` : "—",
            itemName(r.item_id), r.qty, r.reason ?? "—",
            <Badge key="s" tone={STATUS_TONE[r.status] ?? "gray"}>{r.status}</Badge>,
            r.issued_on ?? "—",
            <span key="a" className="flex gap-1">
              {r.status === "REQUESTED" && canApprove && r.requester !== myId && <>
                <Button variant="success" size="sm" disabled={decide.isPending} onClick={() => onDecide(r, true)}>승인</Button>
                <Button variant="danger" size="sm" disabled={decide.isPending} onClick={() => onDecide(r, false)}>반려</Button></>}
              {r.status === "REQUESTED" && r.requester === myId &&
                <Button variant="secondary" size="sm" disabled={cancel.isPending} onClick={() => onCancel(r)}>회수</Button>}
              {r.status === "APPROVED" && canApprove &&
                <Button size="sm" onClick={() => setIssuing(r)}>불출</Button>}
            </span>,
          ])} empty={reqs.isFetching ? "불러오는 중…" : "자재청구 없음"} />
      </Card>

      {creating && (
        <Modal title="자재청구" onClose={() => setCreating(false)}>
          <CreateForm items={items.data?.items ?? []}
            onDone={() => { setCreating(false); refresh(); }} onError={fail} />
        </Modal>
      )}
      {issuing != null && (
        <Modal title={`${issuing.req_no} — 불출(FIFO 출고)`} onClose={() => setIssuing(null)}>
          <IssueForm req={issuing} label={itemName(issuing.item_id)}
            onDone={() => { setIssuing(null); refresh(); }} onError={fail} />
        </Modal>
      )}
    </div>
  );
}

function CreateForm({ items, onDone, onError }: {
  items: Item[]; onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [item, setItem] = useState<Item | null>(null);
  const [qty, setQty] = useState("");
  const [reason, setReason] = useState("");
  const create = useMutation({
    mutationFn: () => api<{ req_no: string }>("/inventory/material-requests", { method: "POST",
      body: JSON.stringify({ item_id: item!.id, qty: Number(qty), reason: reason || null }) }),
    onSuccess: (r) => { toast.ok(`자재청구 접수 — ${r.req_no}`); onDone(); },
    onError,
  });
  return (
    <div className="space-y-2">
      <label className="block text-sm text-slate-600">품목
        <Combobox<Item> value={item} onPick={setItem} options={items} getKey={(it) => it.id}
          getLabel={(it) => `${it.name} (${it.code}·재고 ${it.stock ?? "-"})`}
          getSearch={(it) => `${it.name} ${it.code}`} placeholder="품목 검색" width="w-full" />
      </label>
      {item && item.stock != null && item.stock <= item.safety_stock && (
        <p className="text-xs text-amber-700">현재고 {item.stock} — 안전재고({item.safety_stock}) 이하입니다.</p>)}
      <Input label="수량" value={qty} inputMode="decimal"
        onChange={(e) => setQty(e.target.value.replace(/[^0-9.]/g, ""))} />
      <Input label="사유(선택)" value={reason} onChange={(e) => setReason(e.target.value)}
        placeholder="예: 3병동 주간 소모분" />
      <Button disabled={create.isPending || item == null || !(Number(qty) > 0)}
        onClick={() => create.mutate()}>청구 제출</Button>
    </div>
  );
}

function IssueForm({ req, label, onDone, onError }: {
  req: MReq; label: string; onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [movedOn, setMovedOn] = useState(todayLocal());
  const issue = useMutation({
    mutationFn: () => api(`/inventory/material-requests/${req.id}/issue`, { method: "POST",
      body: JSON.stringify({ moved_on: movedOn || null }) }),
    onSuccess: () => { toast.ok("불출 완료 — FIFO 출고"); onDone(); },
    onError,
  });
  return (
    <div className="space-y-2">
      <p className="text-sm text-slate-700">{label} × <b>{req.qty}</b> — 재고에서 FIFO로 출고합니다.</p>
      <Input label="불출일" type="date" value={movedOn} onChange={(e) => setMovedOn(e.target.value)} />
      <Button disabled={issue.isPending} onClick={() => issue.mutate()}>불출 확정</Button>
    </div>
  );
}
