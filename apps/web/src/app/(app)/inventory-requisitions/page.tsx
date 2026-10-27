"use client";
/** 재고 — 구매요청(PR→발주·0217): 부서 수요 기안(라인) → 승인(SoD·부서경계) → 발주(PO) 전환.
 *  승인/전환은 서버 RBAC(scm.requisition approve — 원장/관리자/부서장DEPT/물류관리자)이 게이트. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { Combobox } from "@/components/combobox";
import { won, wonOrDash } from "@/lib/format";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const STATUS_TONE: Record<string, "gray" | "amber" | "green" | "red" | "blue"> = {
  PENDING: "amber", APPROVED: "green", CONVERTED: "blue", REJECTED: "red", CANCELED: "gray",
};
const APPROVER_ROLES = new Set(["DIRECTOR", "ADMIN", "MANAGER", "LOGISTICS_MANAGER"]);

interface Req {
  id: number; req_no: string; department_id: number | null; requester: number;
  reason: string | null; needed_by: string | null; status: string; po_id: number | null;
}
interface ReqLine {
  item_id: number; item_code: string; item_name: string;
  qty: number; est_price: number | null; note: string | null;
}
interface ReqDetail extends Req { lines: ReqLine[] }
interface Item { id: number; code: string; name: string; stock: number | null; safety_stock: number }  // stock=null: 마약류(권한 없음)
interface Supplier { id: number; name: string }
interface DraftLine { k: number; item: Item | null; qty: string; est_price: string; note: string }
let _lineKey = 0;   // 라인 안정 키 — index key는 중간 삭제 시 Combobox 내부상태가 옆 행으로 잔존

export default function RequisitionsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const myId = Number(auth.claims()?.sub ?? 0);
  const canApprove = APPROVER_ROLES.has(auth.claims()?.role ?? "");

  const [status, setStatus] = useState("");
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);
  const [convertId, setConvertId] = useState<number | null>(null);

  const reqs = useQuery({ queryKey: ["preqs", status], queryFn: () =>
    api<{ items: Req[] }>(`/inventory/requisitions${status ? `?status=${status}` : ""}`) });
  const detail = useQuery({ enabled: detailId != null, queryKey: ["preq", detailId], queryFn: () =>
    api<ReqDetail>(`/inventory/requisitions/${detailId}`) });
  const convertDetail = useQuery({ enabled: convertId != null, queryKey: ["preq", convertId],
    queryFn: () => api<ReqDetail>(`/inventory/requisitions/${convertId}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["preqs"] });
    qc.invalidateQueries({ queryKey: ["preq"] }); };

  const decide = useMutation({
    mutationFn: (v: { id: number; approve: boolean; reason?: string }) =>
      api(`/inventory/requisitions/${v.id}/decide`, { method: "POST",
        body: JSON.stringify({ approve: v.approve, reason: v.reason ?? "" }) }),
    onSuccess: (_r, v) => { toast.ok(v.approve ? "승인" : "반려"); refresh(); },
    onError: fail,
  });
  const cancel = useMutation({
    mutationFn: (id: number) => api(`/inventory/requisitions/${id}/cancel`, { method: "POST" }),
    onSuccess: () => { toast.ok("요청 회수"); refresh(); }, onError: fail,
  });

  const onDecide = async (r: Req, approve: boolean) => {
    if (await confirm({ title: approve ? "구매요청 승인" : "구매요청 반려",
      body: `${r.req_no}을(를) ${approve ? "승인" : "반려"}할까요?`, danger: !approve,
      confirmLabel: approve ? "승인" : "반려" })) decide.mutate({ id: r.id, approve });
  };
  const onCancel = async (r: Req) => {
    if (await confirm({ title: "요청 회수", body: `${r.req_no}을(를) 회수할까요?`,
      confirmLabel: "회수" })) cancel.mutate(r.id);
  };

  const d = detail.data;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-lg font-bold">재고 — 구매요청(PR→발주)</h1>
        <Button onClick={() => setCreating(true)}>구매요청 기안</Button>
      </div>
      <p className="text-xs text-slate-500">
        부서 수요를 기안하면 승인(직무분리 — 기안자 본인 승인 불가·부서장은 본부서만) 후
        공급사·단가를 지정해 발주(PO)로 전환합니다. 승인 전에는 발주가 생성되지 않습니다.</p>

      <QueryErrorBanner queries={[reqs]} />
      <Card title="구매요청" action={
        <select value={status} onChange={(e) => setStatus(e.target.value)}
          className="rounded-md border border-slate-300 px-2 py-1 text-xs">
          <option value="">전체</option>
          {["PENDING", "APPROVED", "CONVERTED", "REJECTED", "CANCELED"].map((s) =>
            <option key={s} value={s}>{s}</option>)}
        </select>}>
        <Table headers={["요청번호", "부서", "사유", "필요일", "상태", "발주", ""]}
          rows={(reqs.data?.items ?? []).map((r) => [
            r.req_no, r.department_id != null ? `부서#${r.department_id}` : "—",
            r.reason ?? "—", r.needed_by ?? "—",
            <Badge key="s" tone={STATUS_TONE[r.status] ?? "gray"}>{r.status}</Badge>,
            r.po_id != null ? `PO#${r.po_id}` : "—",
            <span key="a" className="flex gap-1">
              <Button variant="secondary" size="sm" onClick={() => setDetailId(r.id)}>상세</Button>
              {r.status === "PENDING" && canApprove && r.requester !== myId && <>
                <Button variant="success" size="sm" disabled={decide.isPending} onClick={() => onDecide(r, true)}>승인</Button>
                <Button variant="danger" size="sm" disabled={decide.isPending} onClick={() => onDecide(r, false)}>반려</Button></>}
              {r.status === "PENDING" && r.requester === myId &&
                <Button variant="secondary" size="sm" disabled={cancel.isPending} onClick={() => onCancel(r)}>회수</Button>}
              {r.status === "APPROVED" && canApprove &&
                <Button size="sm" onClick={() => setConvertId(r.id)}>발주 전환</Button>}
            </span>,
          ])} empty={reqs.isFetching ? "불러오는 중…" : "구매요청 없음"} />
      </Card>

      {creating && (
        <Modal title="구매요청 기안" size="lg" dirty onClose={() => setCreating(false)}>
          <CreateForm onDone={() => { setCreating(false); refresh(); }} onError={fail}
            onCancel={() => setCreating(false)} />
        </Modal>
      )}
      {detailId != null && d && (
        <Modal title={`${d.req_no} — 상세`} size="lg" onClose={() => setDetailId(null)}>
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <Badge tone={STATUS_TONE[d.status] ?? "gray"}>{d.status}</Badge>
              {d.needed_by && <span>필요일 <b>{d.needed_by}</b></span>}
              {d.po_id != null && <span>전환 발주 <b>PO#{d.po_id}</b></span>}
              <span className="text-slate-500">{d.reason ?? ""}</span>
            </div>
            <Table headers={["품목", "코드", "수량", "예상단가", "예상금액", "비고"]}
              rows={d.lines.map((ln) => [
                ln.item_name, ln.item_code, ln.qty, wonOrDash(ln.est_price),
                ln.est_price != null ? won(ln.est_price * ln.qty) : "—", ln.note ?? "—",
              ])} empty="라인 없음" />
          </div>
        </Modal>
      )}
      {convertId != null && convertDetail.data && (
        <Modal title={`${convertDetail.data.req_no} — 발주 전환`} size="lg"
          onClose={() => setConvertId(null)}>
          <ConvertForm req={convertDetail.data}
            onDone={() => { setConvertId(null); refresh(); }} onError={fail} />
        </Modal>
      )}
    </div>
  );
}

const newLine = (): DraftLine => ({ k: ++_lineKey, item: null, qty: "", est_price: "", note: "" });

function CreateForm({ onDone, onError, onCancel }: {
  onDone: () => void; onError: (e: unknown) => void; onCancel: () => void;
}) {
  const toast = useToast();
  const [reason, setReason] = useState("");
  const [neededBy, setNeededBy] = useState("");
  const [lines, setLines] = useState<DraftLine[]>([newLine()]);
  const items = useQuery({ queryKey: ["items-all"], queryFn: () =>
    api<{ items: Item[] }>("/inventory/requestable-items") });
  const opts = items.data?.items ?? [];

  const set = (i: number, ln: DraftLine) => setLines(lines.map((x, j) => (j === i ? ln : x)));
  const create = useMutation({
    mutationFn: () => api<{ req_no: string }>("/inventory/requisitions", { method: "POST",
      body: JSON.stringify({
        reason: reason || null, needed_by: neededBy || null,
        lines: lines.map((ln) => ({
          item_id: ln.item!.id, qty: Number(ln.qty),
          est_price: ln.est_price ? Number(ln.est_price) : null,
          note: ln.note || null })) }) }),
    onSuccess: (r) => { toast.ok(`구매요청 기안 — ${r.req_no}`); onDone(); },
    onError,
  });
  const ok = lines.length > 0
    && lines.every((ln) => ln.item != null && Number(ln.qty) > 0);

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-2">
        <Input label="사유" value={reason} onChange={(e) => setReason(e.target.value)}
          placeholder="예: 수액 재고 소진" />
        <Input label="필요일(선택)" type="date" value={neededBy}
          onChange={(e) => setNeededBy(e.target.value)} />
      </div>
      <p className="text-sm text-slate-600">요청 라인</p>
      {lines.map((ln, i) => (
        <div key={ln.k} className="flex flex-wrap items-center gap-2">
          <Combobox<Item> value={ln.item} onPick={(it) => set(i, { ...ln, item: it })}
            options={opts} getKey={(it) => it.id}
            getLabel={(it) => `${it.name} (${it.code}·재고 ${it.stock ?? "-"})`}
            getSearch={(it) => `${it.name} ${it.code}`} placeholder="품목 검색" width="w-64" />
          <Input value={ln.qty} inputMode="decimal" placeholder="수량" className="w-24"
            onChange={(e) => set(i, { ...ln, qty: e.target.value.replace(/[^0-9.]/g, "") })} />
          <Input value={ln.est_price} inputMode="numeric" placeholder="예상단가(선택)" className="w-32"
            onChange={(e) => set(i, { ...ln, est_price: e.target.value.replace(/[^0-9]/g, "") })} />
          <Input value={ln.note} placeholder="비고" className="w-32"
            onChange={(e) => set(i, { ...ln, note: e.target.value })} />
          <Button variant="danger" size="sm" disabled={lines.length <= 1}
            onClick={() => setLines(lines.filter((_, j) => j !== i))}>삭제</Button>
        </div>
      ))}
      <Button variant="secondary" size="sm"
        onClick={() => setLines([...lines, newLine()])}>
        + 라인 추가</Button>
      <div className="flex gap-2">
        <Button disabled={create.isPending || !ok} onClick={() => create.mutate()}>기안 제출</Button>
        <Button variant="secondary" onClick={onCancel}>닫기</Button>
      </div>
    </div>
  );
}

function ConvertForm({ req, onDone, onError }: {
  req: ReqDetail; onDone: () => void; onError: (e: unknown) => void;
}) {
  const toast = useToast();
  const [supplier, setSupplier] = useState<Supplier | null>(null);
  const [prices, setPrices] = useState<Record<number, string>>(() =>
    Object.fromEntries(req.lines.map((ln) => [ln.item_id, ln.est_price != null ? String(ln.est_price) : ""])));
  const suppliers = useQuery({ queryKey: ["suppliers"], queryFn: () =>
    api<{ items: Supplier[] }>("/inventory/suppliers") });

  const convert = useMutation({
    mutationFn: () => api<{ po_id: number }>(`/inventory/requisitions/${req.id}/convert`, {
      method: "POST", body: JSON.stringify({
        supplier_id: supplier!.id,
        unit_prices: Object.fromEntries(
          Object.entries(prices).filter(([, v]) => v !== "").map(([k, v]) => [k, Number(v)])) }) }),
    onSuccess: (r) => { toast.ok(`발주 전환 — PO#${r.po_id}`); onDone(); },
    onError,
  });
  const ok = supplier != null && req.lines.every((ln) => Number(prices[ln.item_id] || 0) > 0);
  const total = req.lines.reduce((a, ln) => a + Number(prices[ln.item_id] || 0) * ln.qty, 0);

  return (
    <div className="space-y-3">
      <label className="block text-sm text-slate-600">공급사
        <Combobox<Supplier> value={supplier} onPick={setSupplier}
          options={suppliers.data?.items ?? []} getKey={(s) => s.id} getLabel={(s) => s.name}
          getSearch={(s) => s.name} placeholder="공급사 검색" width="w-64" />
      </label>
      <Table headers={["품목", "수량", "발주단가", "금액"]}
        rows={req.lines.map((ln) => [
          `${ln.item_name} (${ln.item_code})`, ln.qty,
          <Input key="p" value={prices[ln.item_id] ?? ""} inputMode="numeric" className="w-28"
            onChange={(e) => setPrices({ ...prices,
              [ln.item_id]: e.target.value.replace(/[^0-9]/g, "") })} />,
          won(Number(prices[ln.item_id] || 0) * ln.qty),
        ])} empty="라인 없음" />
      <p className="text-sm">발주 예상금액 <b>{won(total)}</b></p>
      <Button disabled={convert.isPending || !ok} onClick={() => convert.mutate()}>발주 생성</Button>
    </div>
  );
}
