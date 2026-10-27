"use client";
/** 재고 — 의약품 회수·의료폐기물 대장(규제·법정 logging). 회수 등록 시 보유 영향 로트 격리 식별,
 *  폐기물 배출→처리업체 인계(올바로 인계서). scm.items write=ADMIN/부서장. */
import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { todayLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";

const SEVERITY: Record<string, string> = { VOLUNTARY: "자율회수", CLASS1: "1급(위해)",
  CLASS2: "2급", CLASS3: "3급" };
const WASTE_TYPES = ["격리의료", "위해의료", "일반의료", "손상성", "병리계", "생물화학"];

interface Recall {
  id: number; item_id: number; lot_no: string | null; reason: string;
  severity: string; recall_on: string; status: string; created_by: number | null;
  // 영향 로트·수량은 등록 시점에만 계산돼 토스트로 사라졌다 — OPEN 회수건의 "지금 몇 개를
  // 격리해야 하는지"를 다시 볼 방법이 없었다(식약처 대응 법정 업무).
  affected_lots: { lot_no: string; remaining: number }[]; affected_qty: number;
}
interface Waste {
  id: number; waste_type: string; weight_kg: number; stored_on: string;
  handed_over_on: string | null; handler: string | null; manifest_no: string | null; status: string;
}
interface Item { id: number; code: string; name: string }

export default function RecallsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [recalling, setRecalling] = useState(false);
  const [wasting, setWasting] = useState(false);
  const [handover, setHandover] = useState<Waste | null>(null);

  const recalls = useQuery({ queryKey: ["recalls"], queryFn: () =>
    api<{ items: Recall[] }>("/inventory/recalls") });
  const wastes = useQuery({ queryKey: ["waste-logs"], queryFn: () =>
    api<{ items: Waste[] }>("/inventory/waste-logs") });
  const items = useQuery({ queryKey: ["scm-items"], queryFn: () =>
    api<{ items: Item[] }>("/inventory/items?size=1000") });
  const itemName = useMemo(() => Object.fromEntries(
    (items.data?.items ?? []).map((i) => [i.id, `${i.code} ${i.name}`])), [items.data]);
  const refreshR = () => qc.invalidateQueries({ queryKey: ["recalls"] });
  const refreshW = () => qc.invalidateQueries({ queryKey: ["waste-logs"] });

  const closeRecall = useMutation({
    mutationFn: (id: number) => api(`/inventory/recalls/${id}/close`, { method: "POST" }),
    onSuccess: () => { toast.ok("회수 종결"); refreshR(); }, onError: fail });
  const onClose = async (r: Recall) => {
    if (await confirm({ title: "회수 종결", confirmLabel: "종결",
      body: `${itemName[r.item_id] ?? `#${r.item_id}`} 회수를 종결합니다(격리·처리 완료).` }))
      closeRecall.mutate(r.id);
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-bold">재고 — 의약품 회수·의료폐기물 대장</h1>
        <div className="flex gap-2">
          <Button size="sm" onClick={() => setRecalling(true)}>회수 등록</Button>
          <Button size="sm" variant="secondary" onClick={() => setWasting(true)}>폐기물 배출</Button>
        </div>
      </div>
      <p className="text-xs text-slate-500">
        의약품 회수(식약처 회수명령 대응·보유 영향 로트 격리)와 의료폐기물 배출대장(폐기물관리법·
        올바로 인계서)입니다. 법정 기록으로 보존됩니다.</p>

      <QueryErrorBanner queries={[recalls, wastes]} />

      <Card title="의약품 회수">
        <Table headers={["회수일", "품목", "로트", "격리 대상(현재)", "등급", "사유", "상태", ""]}
          rows={(recalls.data?.items ?? []).map((r) => [
            r.recall_on, itemName[r.item_id] ?? `#${r.item_id}`, r.lot_no ?? "전 로트",
            // 조회 시점 기준 잔량 — 회수 진행에 따라 줄어드는 값이라 실시간이어야 의미가 있다
            r.affected_qty > 0
              ? <span key="af" className={r.status === "OPEN" ? "font-semibold text-rose-600" : ""}
                  title={r.affected_lots.map((l) => `${l.lot_no}: ${l.remaining}`).join("\n")}>
                  {r.affected_lots.length}로트 · {r.affected_qty}
                </span>
              : <span key="af" className="text-slate-400">없음</span>,
            <Badge key="s" tone={r.severity === "CLASS1" ? "red" : r.severity.startsWith("CLASS") ? "amber" : "gray"}>
              {SEVERITY[r.severity] ?? r.severity}</Badge>,
            <span key="rs" className="text-xs">{r.reason}</span>,
            <Badge key="st" tone={r.status === "CLOSED" ? "green" : "amber"}>
              {r.status === "CLOSED" ? "종결" : "진행"}</Badge>,
            r.status === "OPEN"
              ? <Button key="c" size="sm" variant="secondary" disabled={closeRecall.isPending}
                  onClick={() => onClose(r)}>종결</Button> : "",
          ])} empty={recalls.isFetching ? "불러오는 중…" : "회수 기록 없음"} />
      </Card>

      <Card title="의료폐기물 배출대장">
        <Table headers={["보관일", "종류", "중량", "인계일", "처리업체", "인계서", "상태"]}
          rows={(wastes.data?.items ?? []).map((w) => [
            w.stored_on, w.waste_type, `${w.weight_kg} kg`,
            w.handed_over_on ?? "—", w.handler ?? "—", w.manifest_no ?? "—",
            w.status === "HANDED_OVER"
              ? <Badge key="s" tone="green">인계완료</Badge>
              : <Button key="h" size="sm" variant="secondary" onClick={() => setHandover(w)}>인계</Button>,
          ])} empty={wastes.isFetching ? "불러오는 중…" : "배출 기록 없음"} />
      </Card>

      {recalling && <RecallModal items={items.data?.items ?? []}
        onDone={() => { setRecalling(false); refreshR(); }} onError={fail}
        onClose={() => setRecalling(false)} />}
      {wasting && <WasteModal onDone={() => { setWasting(false); refreshW(); }}
        onError={fail} onClose={() => setWasting(false)} />}
      {handover && <HandoverModal waste={handover}
        onDone={() => { setHandover(null); refreshW(); }} onError={fail}
        onClose={() => setHandover(null)} />}
    </div>
  );
}

function RecallModal({ items, onDone, onError, onClose }: {
  items: Item[]; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ item_id: "", reason: "", recall_on: todayLocal(),
    lot_no: "", severity: "VOLUNTARY" });
  const m = useMutation({
    mutationFn: () => api<{ affected_qty: number; affected_lots: unknown[] }>(
      "/inventory/recalls", { method: "POST", body: JSON.stringify({
        item_id: Number(f.item_id), reason: f.reason, recall_on: f.recall_on,
        lot_no: f.lot_no || null, severity: f.severity }) }),
    onSuccess: (r) => { toast.ok(`회수 등록 — 영향 로트 ${r.affected_lots.length}건 · ${r.affected_qty} 격리`);
      onDone(); }, onError });
  return (
    <Modal title="의약품 회수 등록" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">품목
          <select value={f.item_id} onChange={(e) => setF({ ...f, item_id: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">선택…</option>
            {items.map((i) => <option key={i.id} value={i.id}>{i.code} {i.name}</option>)}
          </select>
        </label>
        <Input label="로트번호(선택·미입력=전 로트)" value={f.lot_no}
          onChange={(e) => setF({ ...f, lot_no: e.target.value })} />
        <label className="block text-sm text-slate-600">회수 등급
          <select value={f.severity} onChange={(e) => setF({ ...f, severity: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {Object.entries(SEVERITY).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
        </label>
        <Input label="회수일" type="date" value={f.recall_on}
          onChange={(e) => setF({ ...f, recall_on: e.target.value })} />
        <Input label="사유" value={f.reason} onChange={(e) => setF({ ...f, reason: e.target.value })} />
        <Button disabled={m.isPending || !f.item_id || !f.reason.trim()} onClick={() => m.mutate()}>
          회수 등록</Button>
      </div>
    </Modal>
  );
}

function WasteModal({ onDone, onError, onClose }: {
  onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ waste_type: WASTE_TYPES[0], weight_kg: "", stored_on: todayLocal() });
  const m = useMutation({
    mutationFn: () => api("/inventory/waste-logs", { method: "POST", body: JSON.stringify({
      waste_type: f.waste_type, weight_kg: Number(f.weight_kg), stored_on: f.stored_on }) }),
    onSuccess: () => { toast.ok("배출 기록"); onDone(); }, onError });
  return (
    <Modal title="의료폐기물 배출 기록" dirty onClose={onClose}>
      <div className="space-y-2">
        <label className="block text-sm text-slate-600">종류
          <select value={f.waste_type} onChange={(e) => setF({ ...f, waste_type: e.target.value })}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {WASTE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
          </select>
        </label>
        <Input label="중량(kg)" value={f.weight_kg} inputMode="decimal"
          onChange={(e) => setF({ ...f, weight_kg: e.target.value.replace(/[^0-9.]/g, "") })} />
        <Input label="보관일" type="date" value={f.stored_on}
          onChange={(e) => setF({ ...f, stored_on: e.target.value })} />
        <Button disabled={m.isPending || !(Number(f.weight_kg) > 0)} onClick={() => m.mutate()}>
          배출 기록</Button>
      </div>
    </Modal>
  );
}

function HandoverModal({ waste, onDone, onError, onClose }: {
  waste: Waste; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [f, setF] = useState({ handed_over_on: todayLocal(), handler: "", manifest_no: "" });
  const m = useMutation({
    mutationFn: () => api(`/inventory/waste-logs/${waste.id}/handover`, { method: "PATCH",
      body: JSON.stringify({ handed_over_on: f.handed_over_on, handler: f.handler,
        manifest_no: f.manifest_no || null }) }),
    onSuccess: () => { toast.ok("처리업체 인계"); onDone(); }, onError });
  return (
    <Modal title={`폐기물 인계 (${waste.waste_type} ${waste.weight_kg}kg)`} dirty onClose={onClose}>
      <div className="space-y-2">
        <Input label="인계일" type="date" value={f.handed_over_on}
          onChange={(e) => setF({ ...f, handed_over_on: e.target.value })} />
        <Input label="처리업체" value={f.handler}
          onChange={(e) => setF({ ...f, handler: e.target.value })} />
        <Input label="올바로 인계서 번호(선택)" value={f.manifest_no}
          onChange={(e) => setF({ ...f, manifest_no: e.target.value })} />
        <Button disabled={m.isPending || !f.handler.trim()} onClick={() => m.mutate()}>인계 확정</Button>
      </div>
    </Modal>
  );
}
