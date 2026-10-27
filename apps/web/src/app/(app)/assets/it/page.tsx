"use client";
/** IT 자산관리(ITAM) M1 — 하드웨어 대장·배정·QR 태그·사진 증빙·벤더 계약. */
import { useState } from "react";
import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";
import { AttachmentCell } from "@/components/attachment-cell";
import { QrImage, printQrLabel } from "@/components/qr-code";
import { EmployeePicker } from "@/components/employee-picker";
import { useNameMap } from "@/lib/use-name-map";
import { catKo } from "@/lib/itam";

interface Asset {
  id: number; asset_tag: string; name: string; category: string; serial_no: string | null;
  model: string | null; manufacturer: string | null; status: string;
  owner_department_id: number | null; assigned_employee_id: number | null;
  location: string | null; acquired_on: string | null; warranty_until: string | null;
  ip_addr: string | null; hostname: string | null; mac_addr: string | null; handles_phi: boolean; phi_ever: boolean;
  security_grade: string | null; disk_encrypted: boolean | null; av_installed: boolean | null;
  udi: string | null; mfds_no: string | null; calibration_due: string | null;
  finance_asset_id: number | null;
  next_inspection_on: string | null; next_maintenance_on: string | null; return_due_on: string | null;
  spec: Record<string, string> | null; contract_id: number | null; note: string | null;
  last_audited_at: string | null;   // 목록 EP도 반환 — 상세 타입에만 있어 미사용이었다
  assigned_employee_name?: string | null;
}
interface HistItem { id: number; action: string; actor: number | null; at: string;
  before: Record<string, unknown> | null; after: Record<string, unknown> | null }
interface DeptOpt { id: number; name: string }
interface FinAssetOpt { id: number; asset_no: string; name: string }
// PATCH로 직접 설정 불가한 상태(배정=배정기능·폐기=폐기처리) — 편집 상태 옵션에서 제외
const EDITABLE_STATUSES = ["IN_STOCK", "REPAIR", "LOANER", "LOST", "STOLEN", "RETIRED"];
interface Assignment { id: number; employee_id: number; employee_name?: string | null;
  assigned_on: string; returned_on: string | null; acknowledged_at: string | null;
  return_condition: string | null }
interface Maintenance { id: number; kind: string; on_date: string; vendor: string | null;
  cost: number | null; note: string | null }
interface DisposalStep { step_no: number; approver_type: string; approver_value: string | null;
  status: string; acted_at: string | null; note: string | null }
interface DisposalApproval { id: number; status: string; amount: number | null;
  current_step: number; steps: DisposalStep[] }
interface LeaseSummary {
  lease_id: number; lease_no: string; lessor: string | null; start_on: string;
  term_months: number; monthly_payment: number; annual_rate: number;
  rou_book: number; current_liability: number; periods_accrued: number;
  exempt: boolean; status: string;
}
type AssetDetail = Asset & { data_erased: boolean | null; disposed_at: string | null;
  disposal_approval: DisposalApproval | null;
  acquisition_type: string; lease_id: number | null; lease: LeaseSummary | null;
  assigned_employee_name?: string | null; assigned_employee_no?: string | null;
  assignments: Assignment[]; maintenance: Maintenance[] };
const MNT_KINDS = ["REPAIR", "INSPECTION", "UPGRADE", "CALIBRATION"];
const MNT_KO: Record<string, string> = { REPAIR: "수리", INSPECTION: "점검", UPGRADE: "업그레이드", CALIBRATION: "교정" };
interface Contract {
  id: number; vendor: string; title: string; kind: string;
  signed_on: string | null; start_on: string | null; end_on: string | null;
  next_review_on: string | null; amount: number | null;
  counterparty: string | null; counterparty_contact: string | null;
  owner_employee_id: number | null; owner_employee_name?: string | null;
  department_id: number | null; department_name?: string | null;
  reason: string | null; sla_note: string | null; note: string | null;
}
const CONTRACT_KINDS = ["PURCHASE", "AMC", "SUBSCRIPTION", "SLA"];
const KIND_KO: Record<string, string> = {
  PURCHASE: "도입(구매)", AMC: "유지보수(AMC)", SUBSCRIPTION: "정기(구독)", SLA: "SLA" };

const CATEGORIES = ["LAPTOP", "DESKTOP", "SERVER", "NETWORK", "PRINTER", "MOBILE", "MEDICAL_IT", "PERIPHERAL"];
const STATUSES = ["IN_STOCK", "ASSIGNED", "REPAIR", "LOANER", "LOST", "STOLEN", "RETIRED", "DISPOSED"];
const STATUS_KO: Record<string, string> = {
  IN_STOCK: "재고", ASSIGNED: "배정", REPAIR: "수리", LOANER: "대여",
  LOST: "분실", STOLEN: "도난", RETIRED: "불용", DISPOSED: "폐기" };
const STATUS_TONE: Record<string, "gray" | "green" | "red" | "amber"> = {
  IN_STOCK: "gray", ASSIGNED: "green", REPAIR: "amber", LOANER: "amber",
  LOST: "red", STOLEN: "red", RETIRED: "gray", DISPOSED: "gray" };

/** 자산의 만기류(보증·교정·반납·점검) 중 가장 임박한 것 — 목록에서 만기 관리가 가능하도록.
 *  전부 응답에 이미 있으나 상세 모달에만 있어 "이번 달 보증 끝나는 장비"를 뽑을 수 없었다. */
function nearestDue(a: Asset): { label: string; on: string; days: number } | null {
  const cands: [string, string | null][] = [
    ["보증", a.warranty_until], ["교정", a.calibration_due],
    ["반납", a.return_due_on], ["점검", a.next_inspection_on],
  ];
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const rows = cands
    .filter((c): c is [string, string] => !!c[1])
    .map(([label, on]) => ({
      label, on, days: Math.round((new Date(on).getTime() - today.getTime()) / 864e5) }))
    .sort((x, y) => x.days - y.days);
  return rows[0] ?? null;
}

export default function ITAssetPage() {
  const qc = useQueryClient();
  const [cat, setCat] = useState("");
  const [status, setStatus] = useState("");
  const [phi, setPhi] = useState("");        // ""|"1"|"0"
  const [assigned, setAssigned] = useState(""); // ""|"1"|"0"
  const [q, setQ] = useState("");
  const [showNew, setShowNew] = useState(false);
  const [showContracts, setShowContracts] = useState(false);
  const [showProcurement, setShowProcurement] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const list = useQuery({
    queryKey: ["itam-assets", cat, status, phi, assigned, q],
    queryFn: () => {
      const p = new URLSearchParams();
      if (cat) p.set("category", cat);
      if (status) p.set("status", status);
      if (phi) p.set("handles_phi", phi === "1" ? "true" : "false");
      if (assigned) p.set("assigned", assigned === "1" ? "true" : "false");
      if (q.trim()) p.set("q", q.trim());
      return api<{ items: Asset[]; counts: Record<string, number> }>(`/itam/assets?${p}`);
    },
  });
  const counts = list.data?.counts ?? {};
  const total = Object.values(counts).reduce((a, b) => a + b, 0);

  return (
    <div className="space-y-4">
      <QueryErrorBanner queries={[list]} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h1 className="text-lg font-bold text-slate-900">IT 자산관리</h1>
          <p className="text-xs text-slate-500">하드웨어 대장·배정·QR 태그·사진 증빙·벤더 계약 (ITAM M1)</p>
        </div>
        <div className="flex gap-2">
          <Link href="/assets/it/audit"
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">실사(QR)</Link>
          <Link href="/assets/it/bulk"
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">일괄 편집</Link>
          <button onClick={() => {   // 현재 필터 그대로 CSV(감사 제출용·F-10)
              const p = new URLSearchParams();
              if (cat) p.set("category", cat);
              if (status) p.set("status", status);
              if (phi) p.set("handles_phi", phi === "1" ? "true" : "false");
              if (assigned) p.set("assigned", assigned === "1" ? "true" : "false");
              if (q.trim()) p.set("q", q.trim());
              apiDownload(`/itam/assets/export.csv?${p}`, "it-assets.csv");
            }}
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">CSV 내보내기</button>
          <button onClick={() => setShowProcurement(true)}
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">조달 입고 연계</button>
          <button onClick={() => setShowContracts(true)}
            className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-700 hover:bg-slate-50">벤더 계약</button>
          <Button onClick={() => setShowNew(true)}>+ 자산 등록</Button>
        </div>
      </div>

      <div className="flex flex-wrap gap-2 text-xs">
        <button onClick={() => setStatus("")}
          className={`rounded-full px-3 py-1 ${!status ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>
          전체 {total}</button>
        {STATUSES.filter((s) => counts[s]).map((s) => (
          <button key={s} onClick={() => setStatus(s)}
            className={`rounded-full px-3 py-1 ${status === s ? "bg-blue-600 text-white" : "bg-slate-100 text-slate-600"}`}>
            {STATUS_KO[s]} {counts[s]}</button>
        ))}
      </div>

      <Card>
        <div className="mb-3 flex flex-wrap items-end gap-2">
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">분류</span>
            <select value={cat} onChange={(e) => setCat(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">전체</option>
              {CATEGORIES.map((c) => <option key={c} value={c}>{catKo(c)}</option>)}
            </select></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">PHI</span>
            <select value={phi} onChange={(e) => setPhi(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">전체</option><option value="1">PHI 취급</option><option value="0">비PHI</option>
            </select></label>
          <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">배정</span>
            <select value={assigned} onChange={(e) => setAssigned(e.target.value)}
              className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
              <option value="">전체</option><option value="1">배정됨</option><option value="0">미배정</option>
            </select></label>
          <Input label="검색(이름·태그·시리얼)" value={q} placeholder="검색어"
            onChange={(e) => setQ(e.target.value)} />
        </div>
        <Table headers={["태그", "이름", "분류", "상태", "위치", "시리얼", "사용자",
          "만기", "최종실사", "PHI", ""]}
          empty="등록된 IT 자산이 없습니다"
          rows={(list.data?.items ?? []).map((a) => {
            const due = nearestDue(a);
            return [
            <span key="t" className="font-mono text-xs">{a.asset_tag}</span>,
            a.name, catKo(a.category),
            <Badge key="s" tone={STATUS_TONE[a.status]}>{STATUS_KO[a.status]}</Badge>,
            a.location ?? "-",
            a.serial_no ?? "-",
            a.assigned_employee_name ?? (a.assigned_employee_id ? `#${a.assigned_employee_id}` : "-"),
            due
              ? <Badge key="due" tone={due.days < 0 ? "red" : due.days <= 90 ? "amber" : "gray"}>
                  {due.label} {due.on}{due.days < 0 ? " (경과)" : ` (D-${due.days})`}</Badge>
              : "-",
            a.last_audited_at ? a.last_audited_at.slice(0, 10)
              : <span key="na" className="text-amber-600">미실사</span>,
            a.handles_phi ? <Badge key="p" tone="red">PHI</Badge>
              : a.phi_ever ? <Badge key="p" tone="amber">PHI 이력</Badge> : "",
            <button key="d" onClick={() => setDetailId(a.id)}
              className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700 hover:bg-slate-200">상세</button>,
            ];
          })} />
      </Card>

      {showNew && <NewAssetModal onClose={() => setShowNew(false)}
        onDone={(id) => { setShowNew(false); qc.invalidateQueries({ queryKey: ["itam-assets"] }); setDetailId(id); }} />}
      {detailId != null && <DetailModal assetId={detailId} onClose={() => setDetailId(null)}
        onChanged={() => qc.invalidateQueries({ queryKey: ["itam-assets"] })} />}
      {showContracts && <ContractsModal onClose={() => setShowContracts(false)} />}
      {showProcurement && <ProcurementModal onClose={() => setShowProcurement(false)}
        onDone={() => qc.invalidateQueries({ queryKey: ["itam-assets"] })} />}
    </div>
  );
}

interface Candidate { po_line_id: number; po_id: number; item_code: string; item_name: string;
  supplier: string | null; unit_price: number; received_qty: number; registered: number;
  remaining: number; department_id: number | null; received_at: string | null }

/** 조달 입고 연계(R4) — 입고된 구매발주(EQUIPMENT) 중 IT 자산 미등록분을 등록. */
function ProcurementModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const qc = useQueryClient();
  const toast = useToast();
  const [cat, setCat] = useState<Record<number, string>>({});
  const list = useQuery({ queryKey: ["itam-procurement"],
    queryFn: () => api<{ items: Candidate[] }>("/itam/procurement/candidates") });
  const reg = useMutation({
    mutationFn: (c: Candidate) => api(`/itam/procurement/${c.po_line_id}/register`, {
      method: "POST", body: JSON.stringify({ category: cat[c.po_line_id] ?? "PERIPHERAL" }) }),
    onSuccess: (_d, c) => { toast.ok(`${c.item_name} 자산 등록(자동 태그 발번)`);
      qc.invalidateQueries({ queryKey: ["itam-procurement"] }); onDone(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "등록 실패"),
  });

  return (
    <Modal title="조달 입고 연계 — 입고 장비 → IT 자산 등록" onClose={onClose} size="lg">
      <div className="space-y-3">
        <p className="text-xs text-slate-500">구매발주로 입고된 <b>EQUIPMENT(장비)</b> 중 IT 자산으로 미등록된 항목입니다.
          품목명·공급사·도입일·부서가 발주에서 자동 채워지고, 태그는 자동 발번됩니다(등록 후 상세에서 시리얼·PHI 등 보완).</p>
        <Table headers={["발주", "품목", "공급사", "입고", "잔여", "단가", "분류", ""]}
          empty="입고된 미등록 장비가 없습니다"
          rows={(list.data?.items ?? []).map((c) => [
            <span key="p" className="text-xs">#{c.po_id}</span>,
            <span key="n">{c.item_name}<br /><span className="font-mono text-[10px] text-slate-400">{c.item_code}</span></span>,
            c.supplier ?? "-",
            `${c.received_qty}${c.registered ? ` (등록 ${c.registered})` : ""}`,
            <b key="r">{c.remaining}</b>,
            won(c.unit_price),
            <select key="c" value={cat[c.po_line_id] ?? "PERIPHERAL"}
              onChange={(e) => setCat({ ...cat, [c.po_line_id]: e.target.value })}
              className="rounded-md border border-slate-300 px-1.5 py-1 text-xs">
              {CATEGORIES.map((k) => <option key={k} value={k}>{catKo(k)}</option>)}
            </select>,
            <Button key="b" size="sm" disabled={reg.isPending} onClick={() => reg.mutate(c)}>자산 등록</Button>,
          ])} />
      </div>
    </Modal>
  );
}

function ContractsModal({ onClose }: { onClose: () => void }) {
  const qc = useQueryClient();
  const [openId, setOpenId] = useState<number | null>(null);
  const [editing, setEditing] = useState<Contract | null>(null);   // null=신규
  const [showForm, setShowForm] = useState(false);
  const list = useQuery({ queryKey: ["itam-contracts"],
    queryFn: () => api<{ items: Contract[] }>("/itam/contracts") });
  const done = () => { setShowForm(false); setEditing(null);
    qc.invalidateQueries({ queryKey: ["itam-contracts"] }); };

  return (
    <Modal title="벤더 계약 — 도입·유지보수·정기(구독)·SLA" onClose={onClose} size="lg">
      <div className="space-y-4">
        <div className="flex justify-end">
          <Button onClick={() => { setEditing(null); setShowForm(true); }}>+ 계약 등록</Button>
        </div>
        {showForm && <ContractForm contract={editing} onSaved={done}
          onCancel={() => { setShowForm(false); setEditing(null); }} />}

        <Table headers={["벤더 · 계약명", "유형", "체결일", "발효~만기", "검토", "금액", "담당(내부/상대)", "부서", ""]}
          empty="등록된 계약 없음"
          rows={(list.data?.items ?? []).map((c) => [
            <span key="n"><b>{c.vendor}</b><br /><span className="text-xs text-slate-500">{c.title}</span></span>,
            <Badge key="k" tone="blue">{KIND_KO[c.kind] ?? c.kind}</Badge>,
            c.signed_on ?? "-",
            <span key="p" className="text-xs">{c.start_on ?? "?"} ~ {c.end_on ?? "?"}</span>,
            c.next_review_on ?? "-",
            c.amount != null ? won(c.amount) : "-",
            <span key="o" className="text-xs">{c.owner_employee_name ?? "-"}{c.counterparty ? ` / ${c.counterparty}` : ""}</span>,
            c.department_name ?? "-",
            <span key="a" className="flex gap-1">
              <button onClick={() => { setEditing(c); setShowForm(true); }}
                className="rounded bg-blue-100 px-2 py-0.5 text-xs text-blue-700">수정</button>
              <button onClick={() => setOpenId(openId === c.id ? null : c.id)}
                className="rounded bg-slate-100 px-2 py-0.5 text-xs text-slate-700">계약서</button>
            </span>,
          ])} />
        {openId != null && (
          <div className="rounded-lg border border-blue-100 bg-blue-50/40 p-3">
            <p className="mb-1 text-sm font-medium text-slate-700">계약서 첨부 (계약 #{openId})</p>
            <div className="flex items-center gap-2 text-sm text-slate-600">
              <span>📎</span><AttachmentCell entityType="itam_contract" entityId={openId} />
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}

/** 벤더 계약 등록·수정 폼 — 체결·발효·만기·검토 일자, 상대/내부 담당자·부서, 사유·비고. */
function ContractForm({ contract, onSaved, onCancel }: {
  contract: Contract | null; onSaved: () => void; onCancel: () => void }) {
  const c = contract;
  const [f, setF] = useState({
    vendor: c?.vendor ?? "", title: c?.title ?? "", kind: c?.kind ?? "PURCHASE",
    signed_on: c?.signed_on ?? "", start_on: c?.start_on ?? "", end_on: c?.end_on ?? "",
    next_review_on: c?.next_review_on ?? "", amount: c?.amount != null ? String(c.amount) : "",
    counterparty: c?.counterparty ?? "", counterparty_contact: c?.counterparty_contact ?? "",
    owner_employee_id: c?.owner_employee_id ? String(c.owner_employee_id) : "",
    department_id: c?.department_id ? String(c.department_id) : "",
    reason: c?.reason ?? "", sla_note: c?.sla_note ?? "", note: c?.note ?? "" });
  const [err, setErr] = useState("");
  const set = (k: string, v: string) => setF({ ...f, [k]: v });
  const body = () => ({
    vendor: f.vendor, title: f.title, kind: f.kind,
    signed_on: f.signed_on || null, start_on: f.start_on || null, end_on: f.end_on || null,
    next_review_on: f.next_review_on || null, amount: f.amount ? Number(f.amount) : null,
    counterparty: f.counterparty || null, counterparty_contact: f.counterparty_contact || null,
    owner_employee_id: f.owner_employee_id ? Number(f.owner_employee_id) : null,
    department_id: f.department_id ? Number(f.department_id) : null,
    reason: f.reason || null, sla_note: f.sla_note || null, note: f.note || null });
  const save = useMutation({
    mutationFn: () => c
      ? api(`/itam/contracts/${c.id}`, { method: "PATCH", body: JSON.stringify(body()) })
      : api("/itam/contracts", { method: "POST", body: JSON.stringify(body()) }),
    onSuccess: onSaved, onError: (e) => setErr(e instanceof ApiError ? e.message : "저장 실패") });

  return (
    <div className="rounded-lg border border-blue-100 bg-blue-50/30 p-3">
      <p className="mb-2 text-sm font-medium text-blue-800">{c ? "계약 수정" : "계약 등록"}</p>
      <div className="grid grid-cols-2 gap-3">
        <Input label="벤더*" value={f.vendor} onChange={(e) => set("vendor", e.target.value)} />
        <Input label="계약명*" value={f.title} onChange={(e) => set("title", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">유형</span>
          <select value={f.kind} onChange={(e) => set("kind", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {CONTRACT_KINDS.map((k) => <option key={k} value={k}>{KIND_KO[k]}</option>)}
          </select></label>
        <Input label="금액(원)" value={f.amount} onChange={(e) => set("amount", e.target.value)} />
        <Input label="체결일자" type="date" value={f.signed_on} onChange={(e) => set("signed_on", e.target.value)} />
        <Input label="발효(개시)일" type="date" value={f.start_on} onChange={(e) => set("start_on", e.target.value)} />
        <Input label="만기일" type="date" value={f.end_on} onChange={(e) => set("end_on", e.target.value)} />
        <Input label="차기 검토·갱신 시기" type="date" value={f.next_review_on} onChange={(e) => set("next_review_on", e.target.value)} />
        <Input label="상대 담당자" value={f.counterparty} onChange={(e) => set("counterparty", e.target.value)} />
        <Input label="상대 연락처·이메일" value={f.counterparty_contact} onChange={(e) => set("counterparty_contact", e.target.value)} />
        <div className="block"><span className="mb-1 block text-xs font-medium text-slate-600">내부 담당자</span>
          <EmployeePicker value={f.owner_employee_id ? Number(f.owner_employee_id) : null}
            onPick={(id) => set("owner_employee_id", id ? String(id) : "")} /></div>
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">담당 부서</span>
          <DeptSelect value={f.department_id} onChange={(v) => set("department_id", v)} /></label>
        <Input label="사유(도입·계약)" value={f.reason} onChange={(e) => set("reason", e.target.value)} />
        <Input label="SLA 요약" value={f.sla_note} onChange={(e) => set("sla_note", e.target.value)} />
        <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">비고·주요 사항</span>
          <textarea value={f.note} onChange={(e) => set("note", e.target.value)} rows={2}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
      </div>
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
      <div className="mt-2 flex gap-2">
        <Button isLoading={save.isPending} onClick={() => { setErr(""); save.mutate(); }}>저장</Button>
        <button onClick={onCancel} className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-600">취소</button>
      </div>
    </div>
  );
}

function NewAssetModal({ onClose, onDone }: { onClose: () => void; onDone: (id: number) => void }) {
  const qc = useQueryClient();
  const [f, setF] = useState({ asset_tag: "", name: "", category: "LAPTOP", serial_no: "",
    manufacturer: "", model: "", location: "", acquired_on: "", warranty_until: "", handles_phi: false,
    ip_addr: "", hostname: "", contract_id: "", assigned_employee_id: "", owner_department_id: "",
    next_inspection_on: "", next_maintenance_on: "", return_due_on: "",
    cpu: "", ram: "", disk: "", note: "" });
  const [err, setErr] = useState("");
  const [created, setCreated] = useState<Asset | null>(null);
  const contracts = useQuery({ queryKey: ["itam-contracts"],
    queryFn: () => api<{ items: Contract[] }>("/itam/contracts") });
  const genTag = useMutation({
    mutationFn: () => api<{ tag: string }>("/itam/assets/suggest-tag"),
    onSuccess: (r) => setF((prev) => ({ ...prev, asset_tag: r.tag })),
    onError: (e) => setErr(e instanceof ApiError ? e.message : "태그 생성 실패"),
  });

  const create = useMutation({
    mutationFn: () => {
      const spec: Record<string, string> = {};
      if (f.cpu) spec.cpu = f.cpu;
      if (f.ram) spec.ram = f.ram;
      if (f.disk) spec.disk = f.disk;
      return api<Asset>("/itam/assets", { method: "POST", body: JSON.stringify({
        asset_tag: f.asset_tag, name: f.name, category: f.category, handles_phi: f.handles_phi,
        serial_no: f.serial_no || null, manufacturer: f.manufacturer || null, model: f.model || null,
        location: f.location || null, ip_addr: f.ip_addr || null, hostname: f.hostname || null,
        acquired_on: f.acquired_on || null, warranty_until: f.warranty_until || null,
        owner_department_id: f.owner_department_id ? Number(f.owner_department_id) : null,
        next_inspection_on: f.next_inspection_on || null, next_maintenance_on: f.next_maintenance_on || null,
        return_due_on: f.return_due_on || null,
        assigned_employee_id: f.assigned_employee_id ? Number(f.assigned_employee_id) : null,
        spec: Object.keys(spec).length ? spec : null, note: f.note || null,
        contract_id: f.contract_id ? Number(f.contract_id) : null }) });
    },
    onSuccess: (a) => setCreated(a),
    onError: (e) => setErr(e instanceof ApiError ? e.message : "등록 실패"),
  });

  const set = (k: string, v: string | boolean) => setF({ ...f, [k]: v });

  return (
    <Modal title={created ? "자산 등록 완료 — QR·사진" : "IT 자산 등록"}
      onClose={() => {   // X 닫기여도 이미 생성된 자산은 목록 반영(중복 등록 방지)
        if (created) qc.invalidateQueries({ queryKey: ["itam-assets"] });
        onClose();
      }} size="lg">
      {!created ? (
        <div className="space-y-3">
          <div className="grid grid-cols-2 gap-3">
            <div className="col-span-2 flex items-center gap-3 rounded-lg border border-slate-100 bg-slate-50/50 p-2">
              <div className="flex-1">
                <div className="flex items-end gap-2">
                  <Input label="자산 태그(QR)*" value={f.asset_tag} placeholder="IT-000001"
                    onChange={(e) => set("asset_tag", e.target.value)} />
                  <Button type="button" variant="neutral" size="sm" onClick={() => genTag.mutate()} disabled={genTag.isPending}>QR 자동 생성</Button>
                </div>
                <p className="mt-1 text-[11px] text-slate-400">자동 생성 시 다음 순번(IT-NNNNNN)이 부여됩니다. 등록 후 라벨 인쇄.</p>
              </div>
              {f.asset_tag.trim() && (
                <div className="text-center">
                  <QrImage value={f.asset_tag.trim()} size={72} />
                  <p className="mt-0.5 font-mono text-[10px] text-slate-500">{f.asset_tag.trim()}</p>
                </div>
              )}
            </div>
            <Input label="이름*" value={f.name} onChange={(e) => set("name", e.target.value)} />
            <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">분류</span>
              <select value={f.category} onChange={(e) => set("category", e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                {CATEGORIES.map((c) => <option key={c} value={c}>{catKo(c)}</option>)}
              </select></label>
            <Input label="시리얼" value={f.serial_no} onChange={(e) => set("serial_no", e.target.value)} />
            <Input label="제조사" value={f.manufacturer} onChange={(e) => set("manufacturer", e.target.value)} />
            <Input label="모델" value={f.model} onChange={(e) => set("model", e.target.value)} />
            <Input label="배치위치" value={f.location} onChange={(e) => set("location", e.target.value)} />
            <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">소유부서</span>
              <DeptSelect value={f.owner_department_id} onChange={(v) => set("owner_department_id", v)} /></label>
            <div className="block"><span className="mb-1 block text-xs font-medium text-slate-600">사용자(직원)</span>
              <EmployeePicker value={f.assigned_employee_id ? Number(f.assigned_employee_id) : null}
                onPick={(id) => set("assigned_employee_id", id ? String(id) : "")} /></div>
            <Input label="도입일자" type="date" value={f.acquired_on}
              onChange={(e) => set("acquired_on", e.target.value)} />
            <Input label="보증만기" type="date" value={f.warranty_until}
              onChange={(e) => set("warranty_until", e.target.value)} />
            <Input label="정기점검 예정일" type="date" value={f.next_inspection_on}
              onChange={(e) => set("next_inspection_on", e.target.value)} />
            <Input label="정비 예정일" type="date" value={f.next_maintenance_on}
              onChange={(e) => set("next_maintenance_on", e.target.value)} />
            <Input label="회수 예정일(대여·임시)" type="date" value={f.return_due_on}
              onChange={(e) => set("return_due_on", e.target.value)} />
            <Input label="IP" value={f.ip_addr} onChange={(e) => set("ip_addr", e.target.value)} />
            <Input label="호스트명" value={f.hostname} onChange={(e) => set("hostname", e.target.value)} />
            <Input label="장비 디테일 · CPU" value={f.cpu} onChange={(e) => set("cpu", e.target.value)} />
            <Input label="RAM" value={f.ram} onChange={(e) => set("ram", e.target.value)} />
            <Input label="디스크" value={f.disk} onChange={(e) => set("disk", e.target.value)} />
            <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">벤더 계약</span>
              <select value={f.contract_id} onChange={(e) => set("contract_id", e.target.value)}
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                <option value="">없음</option>
                {(contracts.data?.items ?? []).map((c) => (
                  <option key={c.id} value={c.id}>{c.vendor} · {c.title} ({c.kind})</option>))}
              </select></label>
            <label className="flex items-end gap-2 pb-1 text-sm text-slate-700">
              <input type="checkbox" checked={f.handles_phi}
                onChange={(e) => set("handles_phi", e.target.checked)} />
              환자정보(PHI) 취급 장비</label>
            <label className="col-span-2 block"><span className="mb-1 block text-xs font-medium text-slate-600">비고·메모</span>
              <textarea value={f.note} onChange={(e) => set("note", e.target.value)} rows={2}
                placeholder="구매 경위·상태·특이사항 등"
                className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
          </div>
          <p className="text-[11px] text-slate-400">📷 실물·라벨 사진은 등록 완료 후 이미지 첨부에서 등록합니다.</p>
          {err && <p className="text-xs text-red-600">{err}</p>}
          <div className="flex gap-2">
            <Button isLoading={create.isPending}
              onClick={() => { setErr(""); create.mutate(); }}>등록</Button>
            <button onClick={onClose} className="rounded-md border border-slate-300 px-4 py-1.5 text-sm text-slate-600">취소</button>
          </div>
        </div>
      ) : (
        <div className="space-y-4">
          <div className="flex items-center gap-4 rounded-lg border border-slate-100 bg-slate-50/50 p-4">
            <QrImage value={created.asset_tag} size={130} />
            <div className="text-sm">
              <p className="font-mono font-semibold">{created.asset_tag}</p>
              <p className="text-slate-700">{created.name} · {catKo(created.category)}</p>
              <Button size="sm" className="mt-2" onClick={() => printQrLabel(created.asset_tag,
                [created.name, catKo(created.category)])}>QR 라벨 인쇄</Button>
            </div>
          </div>
          <div>
            <p className="mb-1 text-sm font-medium text-slate-700">📷 사진·이미지 등록(실물·시리얼·구매증빙)</p>
            <AttachmentCell entityType="it_asset" entityId={created.id} gallery />
            <p className="mt-1 text-[11px] text-slate-400">이미지(PNG·JPG·WEBP)·PDF 등록. 썸네일 클릭 시 원본 보기.</p>
          </div>
          <Button onClick={() => onDone(created.id)}>완료</Button>
        </div>
      )}
    </Modal>
  );
}

/** 소유부서 드롭다운 — /departments (ADMIN/DIRECTOR/MANAGER read). */
function DeptSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const depts = useQuery({ queryKey: ["departments"],
    queryFn: () => api<{ items: DeptOpt[] }>("/departments") });
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
      <option value="">없음</option>
      {(depts.data?.items ?? []).map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
    </select>
  );
}

/** 감가상각 연결 — finance.FixedAsset 드롭다운(F-5). 회계 대장과 1:1 연결만(중복 입력 없음). */
function FinAssetSelect({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const assets = useQuery({ queryKey: ["finance-assets"],
    queryFn: () => api<{ items: FinAssetOpt[] }>("/finance/assets") });
  return (
    <select value={value} onChange={(e) => onChange(e.target.value)}
      className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
      <option value="">연결 없음</option>
      {(assets.data?.items ?? []).map((a) => (
        <option key={a.id} value={a.id}>{a.asset_no} · {a.name}</option>))}
    </select>
  );
}

function DetailModal({ assetId, onClose, onChanged }: {
  assetId: number; onClose: () => void; onChanged: () => void }) {
  const qc = useQueryClient();
  const ask = useConfirm();
  const toast = useToast();
  const [empId, setEmpId] = useState("");
  const [mnt, setMnt] = useState({ kind: "REPAIR", on_date: "", vendor: "", cost: "" });
  const [erased, setErased] = useState(false);
  const [editing, setEditing] = useState(false);
  const [showHist, setShowHist] = useState(false);
  const detail = useQuery({ queryKey: ["itam-asset", assetId],
    queryFn: () => api<AssetDetail>(`/itam/assets/${assetId}`) });
  const deptName = useNameMap("departments", "/departments");   // 소유부서 ID→이름(X2-6)
  const refresh = () => { qc.invalidateQueries({ queryKey: ["itam-asset", assetId] });
    qc.invalidateQueries({ queryKey: ["itam-assets"] }); onChanged(); };
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.message : "요청 실패");
  const assign = useMutation({
    mutationFn: () => api(`/itam/assets/${assetId}/assign`, { method: "POST",
      body: JSON.stringify({ employee_id: Number(empId) }) }),
    onSuccess: () => { setEmpId(""); toast.ok("배정 완료"); refresh(); }, onError: fail });
  const ret = useMutation({
    mutationFn: () => api(`/itam/assets/${assetId}/return`, { method: "POST", body: JSON.stringify({}) }),
    onSuccess: () => { toast.ok("반납 완료"); refresh(); }, onError: fail });
  const addMnt = useMutation({
    mutationFn: () => api(`/itam/assets/${assetId}/maintenance`, { method: "POST", body: JSON.stringify({
      kind: mnt.kind, on_date: mnt.on_date, vendor: mnt.vendor || null,
      cost: mnt.cost ? Number(mnt.cost) : null }) }),
    onSuccess: () => { setMnt({ kind: "REPAIR", on_date: "", vendor: "", cost: "" }); toast.ok("정비 이력 추가"); refresh(); },
    onError: fail });
  const dispose = useMutation({
    mutationFn: () => api(`/itam/assets/${assetId}/dispose`, { method: "POST",
      body: JSON.stringify({ data_erased: erased }) }),
    onSuccess: () => { toast.ok("폐기 처리 완료"); refresh(); },
    onError: (e) => toast.err(e instanceof ApiError ? e.message : "폐기 실패") });
  const disposeApproval = useMutation({
    mutationFn: (approve: boolean) => api(`/itam/assets/${assetId}/dispose-approval`, {
      method: "POST", body: JSON.stringify({ approve }) }),
    onSuccess: () => { toast.ok("폐기 결재 처리"); refresh(); }, onError: fail });
  const [lease, setLease] = useState({ start_on: "", term_months: "", monthly_payment: "",
    annual_rate: "", lessor: "", exempt: false });
  const attachLease = useMutation({
    mutationFn: () => api(`/itam/assets/${assetId}/lease`, { method: "POST", body: JSON.stringify({
      start_on: lease.start_on, term_months: Number(lease.term_months),
      monthly_payment: Number(lease.monthly_payment),
      annual_rate: lease.annual_rate ? Number(lease.annual_rate) : 0,
      lessor: lease.lessor || "", exempt: lease.exempt }) }),
    onSuccess: () => { toast.ok("리스 취득 — IFRS16 리스계약 개시·연결"); refresh(); },
    onError: fail });

  const a = detail.data;
  return (
    <Modal title={a ? `${a.asset_tag} · ${a.name}` : "자산 상세"} onClose={onClose} size="lg">
      {!a ? <p className="text-sm text-slate-400">불러오는 중…</p> : (
        <div className="space-y-4">
          <div className="flex gap-4">
            <QrImage value={a.asset_tag} size={110} />
            <div className="flex-1 text-sm text-slate-700">
              <p><b>상태</b> {STATUS_KO[a.status]} {a.handles_phi
                ? <Badge tone="red">PHI</Badge>
                : a.phi_ever ? <Badge tone="amber">PHI 이력</Badge> : null}</p>
              <p><b>분류</b> {catKo(a.category)} · <b>시리얼</b> {a.serial_no ?? "-"}</p>
              <p><b>제조/모델</b> {a.manufacturer ?? "-"} {a.model ?? ""}</p>
              <p><b>네트워크</b> {a.ip_addr ?? "-"} / {a.hostname ?? "-"} / {a.mac_addr ?? "-"}</p>
              <p><b>도입일</b> {a.acquired_on ?? "-"} · <b>보증만기</b> {a.warranty_until ?? "-"} · <b>위치</b> {a.location ?? "-"}</p>
              <p><b>정기점검</b> {a.next_inspection_on ?? "-"} · <b>정비예정</b> {a.next_maintenance_on ?? "-"} · <b>회수예정</b> {a.return_due_on ?? "-"}</p>
              <p><b>최근 실사</b> {a.last_audited_at ? a.last_audited_at.slice(0, 10) : "실사 이력 없음"}</p>
              <p><b>소유부서</b> {deptName(a.owner_department_id)} · <b>보안등급</b> {a.security_grade ?? "-"}
                {" "}· <b>암호화</b> {a.disk_encrypted == null ? "-" : a.disk_encrypted ? "✓" : "✗"}
                {" "}· <b>백신</b> {a.av_installed == null ? "-" : a.av_installed ? "✓" : "✗"}</p>
              {(a.udi || a.mfds_no || a.calibration_due) &&
                <p><b>의료기기</b> UDI {a.udi ?? "-"} · 허가 {a.mfds_no ?? "-"} · 교정만기 {a.calibration_due ?? "-"}</p>}
              <p><b>취득유형</b> {a.acquisition_type === "LEASED"
                ? <Badge tone="blue">리스(IFRS16)</Badge> : "구매/자체"}
                {" "}· <b>감가상각 연결</b> {a.finance_asset_id ? `고정자산 #${a.finance_asset_id}` : "미연결"}</p>
              {a.spec && Object.keys(a.spec).length > 0 &&
                <p><b>장비 디테일</b> {Object.entries(a.spec).map(([k, v]) => `${k}:${v}`).join(" · ")}</p>}
              <div className="mt-1 flex flex-wrap gap-1.5">
                <Button variant="neutral" size="sm" onClick={() => printQrLabel(a.asset_tag, [a.name, catKo(a.category)])}>QR 라벨 인쇄</Button>
                {a.status !== "DISPOSED" &&
                  <Button size="sm" onClick={() => setEditing((v) => !v)}>{editing ? "편집 닫기" : "편집"}</Button>}
                <button onClick={() => setShowHist((v) => !v)}
                  className="rounded border border-slate-300 px-2.5 py-1 text-xs text-slate-700">{showHist ? "이력 닫기" : "변경 이력"}</button>
              </div>
            </div>
          </div>

          {editing && <EditAssetForm a={a} onSaved={() => { setEditing(false); refresh(); }} />}
          {showHist && <HistoryPanel path={`/itam/assets/${assetId}/history`} />}

          <div className="rounded-lg border border-slate-100 p-3">
            <p className="mb-2 text-sm font-medium text-slate-700">리스(IFRS16)</p>
            {a.lease ? (
              <div className="text-sm text-slate-700">
                <p><b>{a.lease.lease_no}</b> · {a.lease.lessor ?? "리스사 미지정"}
                  {a.lease.exempt && <Badge tone="amber">면제(단기·소액)</Badge>}
                  {" "}<Badge tone={a.lease.status === "ACTIVE" ? "green" : "gray"}>{a.lease.status}</Badge></p>
                <p><b>개시</b> {a.lease.start_on} · <b>기간</b> {a.lease.term_months}개월
                  {" "}· <b>월 리스료</b> {won(a.lease.monthly_payment)} · <b>이자율</b> {(a.lease.annual_rate * 100).toFixed(2)}%</p>
                <p><b>사용권자산(장부)</b> {won(a.lease.rou_book)} · <b>리스부채(잔액)</b> {won(a.lease.current_liability)}
                  {" "}· <b>경과</b> {a.lease.periods_accrued}/{a.lease.term_months}회</p>
                <p className="mt-1 text-xs text-slate-400">월 이자·상각은 재무 &gt; 리스 월마감에서 전기됩니다.</p>
              </div>
            ) : a.finance_asset_id ? (
              <p className="text-sm text-slate-400">구매(자체 취득) 자산 — 리스 연결 불가.</p>
            ) : a.status === "DISPOSED" ? (
              <p className="text-sm text-slate-400">폐기 자산.</p>
            ) : (
              <div className="space-y-2">
                <p className="text-xs text-slate-500">리스로 취득한 장비면 리스계약을 개시·연결합니다
                  (사용권자산·리스부채 인식). 미지정 시 리스번호는 ITL-{a.asset_tag}.</p>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
                  <Input label="개시일" type="date" value={lease.start_on}
                    onChange={(e) => setLease({ ...lease, start_on: e.target.value })} />
                  <Input label="기간(개월)" inputMode="numeric" value={lease.term_months}
                    onChange={(e) => setLease({ ...lease, term_months: e.target.value.replace(/[^0-9]/g, "") })} />
                  <Input label="월 리스료" inputMode="numeric" value={lease.monthly_payment}
                    onChange={(e) => setLease({ ...lease, monthly_payment: e.target.value.replace(/[^0-9]/g, "") })} />
                  <Input label="연이자율(예:0.05)" inputMode="decimal" value={lease.annual_rate}
                    onChange={(e) => setLease({ ...lease, annual_rate: e.target.value.replace(/[^0-9.]/g, "") })} />
                  <Input label="리스사" value={lease.lessor}
                    onChange={(e) => setLease({ ...lease, lessor: e.target.value })} />
                  <label className="flex items-center gap-1 self-end text-sm text-slate-600">
                    <input type="checkbox" checked={lease.exempt}
                      onChange={(e) => setLease({ ...lease, exempt: e.target.checked })} />
                    단기·소액 면제
                  </label>
                </div>
                <Button size="sm" isLoading={attachLease.isPending}
                  disabled={!lease.start_on || !(Number(lease.term_months) > 0) || !(Number(lease.monthly_payment) > 0)}
                  onClick={() => attachLease.mutate()}>리스 취득 연결</Button>
              </div>
            )}
          </div>


          <div className="rounded-lg border border-slate-100 p-3">
            <p className="mb-2 text-sm font-medium text-slate-700">배정 / 반납</p>
            {a.assigned_employee_id ? (
              <div className="flex items-center gap-2 text-sm">
                <span>현재 사용자 <b>{a.assigned_employee_name ?? `#${a.assigned_employee_id}`}</b>
                  {a.assigned_employee_no && <span className="text-slate-400"> ({a.assigned_employee_no})</span>}</span>
                <Button variant="warning" size="sm" onClick={() => ret.mutate()}>반납</Button>
              </div>
            ) : (
              <div className="flex items-end gap-2">
                <div><span className="mb-1 block text-xs font-medium text-slate-600">사용자(직원)</span>
                  <EmployeePicker value={empId ? Number(empId) : null}
                    onPick={(id) => setEmpId(id ? String(id) : "")} /></div>
                <Button size="sm" disabled={!empId || assign.isPending} onClick={() => assign.mutate()}>배정</Button>
              </div>
            )}
            <Table headers={["직원", "배정일", "반납일", "수령확인"]} empty="배정 이력 없음"
              rows={a.assignments.map((x) => [
                x.employee_name ?? `#${x.employee_id}`, x.assigned_on, x.returned_on ?? "사용중",
                x.acknowledged_at ? "✓" : "-"])} />
          </div>

          <div className="rounded-lg border border-slate-100 p-3">
            <p className="mb-2 text-sm font-medium text-slate-700">유지보수 이력</p>
            <div className="flex flex-wrap items-end gap-2">
              <label className="block"><span className="mb-1 block text-xs text-slate-600">유형</span>
                <select value={mnt.kind} onChange={(e) => setMnt({ ...mnt, kind: e.target.value })}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm">
                  {MNT_KINDS.map((k) => <option key={k} value={k}>{MNT_KO[k]}</option>)}
                </select></label>
              <label className="block"><span className="mb-1 block text-xs text-slate-600">일자</span>
                <input type="date" value={mnt.on_date} onChange={(e) => setMnt({ ...mnt, on_date: e.target.value })}
                  className="rounded-md border border-slate-300 px-2 py-1.5 text-sm" /></label>
              <Input label="벤더" value={mnt.vendor} onChange={(e) => setMnt({ ...mnt, vendor: e.target.value })} />
              <Input label="비용(원)" value={mnt.cost} onChange={(e) => setMnt({ ...mnt, cost: e.target.value })} />
              <Button variant="neutral" size="sm" disabled={!mnt.on_date || addMnt.isPending} onClick={() => addMnt.mutate()}>기록</Button>
            </div>
            <Table headers={["유형", "일자", "벤더", "비용"]} empty="유지보수 이력 없음"
              rows={a.maintenance.map((m) => [
                MNT_KO[m.kind] ?? m.kind, m.on_date, m.vendor ?? "-",
                m.cost != null ? `${m.cost.toLocaleString()} 원` : "-"])} />
          </div>

          {a.note && <p className="rounded-lg bg-slate-50 px-3 py-2 text-sm text-slate-600"><b>비고</b> {a.note}</p>}

          <div>
            <p className="mb-1 text-sm font-medium text-slate-700">📷 사진·이미지·증빙 서류</p>
            <AttachmentCell entityType="it_asset" entityId={assetId} gallery />
          </div>

          {a.status !== "DISPOSED" ? (
            <div className="rounded-lg border border-red-100 bg-red-50/40 p-3">
              <p className="mb-1 text-sm font-medium text-red-700">폐기</p>
              {a.disposal_approval && a.disposal_approval.status === "PENDING" ? (
                <div className="space-y-2">
                  <p className="text-sm text-amber-700">⏳ 폐기 결재 진행 중 (단계 {a.disposal_approval.current_step + 1}/{a.disposal_approval.steps.length})
                    {a.disposal_approval.amount ? ` · 금액 ${won(a.disposal_approval.amount)}` : ""}</p>
                  <div className="flex gap-2">
                    <Button variant="success" size="sm" disabled={disposeApproval.isPending} onClick={() => disposeApproval.mutate(true)}>결재 승인</Button>
                    <Button variant="neutral" size="sm" disabled={disposeApproval.isPending} onClick={() => disposeApproval.mutate(false)}>반려</Button>
                  </div>
                  <p className="text-[11px] text-slate-500">승인 완료 후 [폐기 처리]를 다시 누르면 확정됩니다(직무분리: 상신자 본인 승인 불가).</p>
                </div>
              ) : (
                <>
                  {a.disposal_approval?.status === "APPROVED" &&
                    <p className="text-sm text-emerald-700">✓ 폐기 결재 승인됨 — [폐기 처리]로 확정하세요.</p>}
                  {/* 서버 게이트가 `handles_phi or phi_ever`(sticky)라 조건을 동일하게 맞춘다 —
                      handles_phi만 보면 과거 취급 장비는 체크박스 없이 폐기가 영구 400이 된다. */}
                  {(a.handles_phi || a.phi_ever) && (
                    <label className="flex items-center gap-2 text-sm text-slate-700">
                      <input type="checkbox" checked={erased} onChange={(e) => setErased(e.target.checked)} />
                      <b>{a.handles_phi ? "PHI 취급 장비" : "과거 PHI 취급 이력"}</b>
                      {" — 데이터 소거 완료를 확인합니다(소거 확인서 첨부 권장)"}</label>
                  )}
                  <div className="mt-2">
                    <Button variant="danger" size="sm" disabled={dispose.isPending} onClick={async () => {
                        if (await ask({ title: "IT 자산 폐기", body: "이 자산을 폐기 처리할까요?",
                          danger: true, confirmLabel: "폐기 처리" })) dispose.mutate(); }}>폐기 처리</Button>
                  </div>
                  {a.handles_phi && <p className="mt-1 text-[11px] text-red-500">PHI 장비는 소거 확인 없이는 폐기할 수 없습니다.</p>}
                  <p className="mt-1 text-[11px] text-slate-400">고액(연결 고정자산 장부가) 폐기는 결재 규칙에 따라 승인 후 확정됩니다.</p>
                </>
              )}
            </div>
          ) : (
            <p className="text-sm text-slate-500">폐기 완료 ({a.disposed_at}) · 데이터 소거 {a.data_erased ? "✓" : "미확인"}</p>
          )}
        </div>
      )}
    </Modal>
  );
}

/** 자산 개별 편집(F-4) — 소유부서·보안·의료기기·감가상각 연결 등 등록 후 수정. PATCH /itam/assets/{id}. */
function EditAssetForm({ a, onSaved }: { a: AssetDetail; onSaved: () => void }) {
  const contracts = useQuery({ queryKey: ["itam-contracts"],
    queryFn: () => api<{ items: Contract[] }>("/itam/contracts") });
  const tri = (b: boolean | null) => (b == null ? "" : b ? "1" : "0");
  const [f, setF] = useState({
    name: a.name, category: a.category, status: a.status, manufacturer: a.manufacturer ?? "",
    model: a.model ?? "", owner_department_id: a.owner_department_id ? String(a.owner_department_id) : "",
    location: a.location ?? "", warranty_until: a.warranty_until ?? "", ip_addr: a.ip_addr ?? "",
    hostname: a.hostname ?? "", mac_addr: a.mac_addr ?? "", handles_phi: a.handles_phi,
    security_grade: a.security_grade ?? "", disk_encrypted: tri(a.disk_encrypted),
    av_installed: tri(a.av_installed), udi: a.udi ?? "", mfds_no: a.mfds_no ?? "",
    calibration_due: a.calibration_due ?? "", next_inspection_on: a.next_inspection_on ?? "",
    next_maintenance_on: a.next_maintenance_on ?? "", return_due_on: a.return_due_on ?? "",
    finance_asset_id: a.finance_asset_id ? String(a.finance_asset_id) : "",
    contract_id: a.contract_id ? String(a.contract_id) : "", note: a.note ?? "" });
  const [err, setErr] = useState("");
  const set = (k: string, v: string | boolean) => setF({ ...f, [k]: v });
  const triVal = (v: string) => (v === "" ? null : v === "1");
  const save = useMutation({
    mutationFn: () => api(`/itam/assets/${a.id}`, { method: "PATCH", body: JSON.stringify({
      name: f.name, category: f.category, status: f.status,
      manufacturer: f.manufacturer || null, model: f.model || null,
      owner_department_id: f.owner_department_id ? Number(f.owner_department_id) : null,
      location: f.location || null, warranty_until: f.warranty_until || null,
      ip_addr: f.ip_addr || null, hostname: f.hostname || null, mac_addr: f.mac_addr || null,
      handles_phi: f.handles_phi, security_grade: f.security_grade || null,
      disk_encrypted: triVal(f.disk_encrypted), av_installed: triVal(f.av_installed),
      udi: f.udi || null, mfds_no: f.mfds_no || null, calibration_due: f.calibration_due || null,
      next_inspection_on: f.next_inspection_on || null, next_maintenance_on: f.next_maintenance_on || null,
      return_due_on: f.return_due_on || null,
      finance_asset_id: f.finance_asset_id ? Number(f.finance_asset_id) : null,
      contract_id: f.contract_id ? Number(f.contract_id) : null, note: f.note || null }) }),
    onSuccess: onSaved, onError: (e) => setErr(e instanceof ApiError ? e.message : "저장 실패") });
  const triSel = (k: string, label: string) => (
    <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">{label}</span>
      <select value={f[k as keyof typeof f] as string} onChange={(e) => set(k, e.target.value)}
        className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
        <option value="">미확인</option><option value="1">예</option><option value="0">아니오</option>
      </select></label>);

  return (
    <div className="rounded-lg border border-blue-100 bg-blue-50/30 p-3">
      <p className="mb-2 text-sm font-medium text-blue-800">자산 편집</p>
      <div className="grid grid-cols-2 gap-3">
        <Input label="이름" value={f.name} onChange={(e) => set("name", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">분류</span>
          <select value={f.category} onChange={(e) => set("category", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {CATEGORIES.map((c) => <option key={c} value={c}>{catKo(c)}</option>)}
          </select></label>
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">상태</span>
          <select value={EDITABLE_STATUSES.includes(f.status) ? f.status : ""}
            onChange={(e) => set("status", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            {!EDITABLE_STATUSES.includes(f.status) && <option value="">{STATUS_KO[f.status]}(배정/폐기는 전용 기능)</option>}
            {EDITABLE_STATUSES.map((s) => <option key={s} value={s}>{STATUS_KO[s]}</option>)}
          </select></label>
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">소유부서</span>
          <DeptSelect value={f.owner_department_id} onChange={(v) => set("owner_department_id", v)} /></label>
        <Input label="제조사" value={f.manufacturer} onChange={(e) => set("manufacturer", e.target.value)} />
        <Input label="모델" value={f.model} onChange={(e) => set("model", e.target.value)} />
        <Input label="배치위치" value={f.location} onChange={(e) => set("location", e.target.value)} />
        <Input label="보증만기" type="date" value={f.warranty_until} onChange={(e) => set("warranty_until", e.target.value)} />
        <Input label="IP" value={f.ip_addr} onChange={(e) => set("ip_addr", e.target.value)} />
        <Input label="호스트명" value={f.hostname} onChange={(e) => set("hostname", e.target.value)} />
        <Input label="MAC" value={f.mac_addr} onChange={(e) => set("mac_addr", e.target.value)} />
        <Input label="보안등급" value={f.security_grade} onChange={(e) => set("security_grade", e.target.value)} />
        {triSel("disk_encrypted", "디스크 암호화")}
        {triSel("av_installed", "백신 설치")}
        <Input label="UDI(의료기기)" value={f.udi} onChange={(e) => set("udi", e.target.value)} />
        <Input label="허가번호(MFDS)" value={f.mfds_no} onChange={(e) => set("mfds_no", e.target.value)} />
        <Input label="교정만기" type="date" value={f.calibration_due} onChange={(e) => set("calibration_due", e.target.value)} />
        <Input label="정기점검 예정" type="date" value={f.next_inspection_on} onChange={(e) => set("next_inspection_on", e.target.value)} />
        <Input label="정비 예정" type="date" value={f.next_maintenance_on} onChange={(e) => set("next_maintenance_on", e.target.value)} />
        <Input label="회수 예정" type="date" value={f.return_due_on} onChange={(e) => set("return_due_on", e.target.value)} />
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">감가상각 연결(고정자산)</span>
          <FinAssetSelect value={f.finance_asset_id} onChange={(v) => set("finance_asset_id", v)} /></label>
        <label className="block"><span className="mb-1 block text-xs font-medium text-slate-600">벤더 계약</span>
          <select value={f.contract_id} onChange={(e) => set("contract_id", e.target.value)}
            className="w-full rounded-md border border-slate-300 px-2 py-1.5 text-sm">
            <option value="">없음</option>
            {(contracts.data?.items ?? []).map((c) => (
              <option key={c.id} value={c.id}>{c.vendor} · {c.title}</option>))}
          </select></label>
        <label className="col-span-2 flex items-center gap-2 text-sm text-slate-700">
          <input type="checkbox" checked={f.handles_phi} onChange={(e) => set("handles_phi", e.target.checked)} />
          환자정보(PHI) 취급 장비 <span className="text-[11px] text-slate-400">(한 번 지정 시 폐기 소거 게이트 영구 유지)</span></label>
        <Input label="비고" value={f.note} onChange={(e) => set("note", e.target.value)} />
      </div>
      {err && <p className="mt-2 text-xs text-red-600">{err}</p>}
      <div className="mt-2">
        <Button isLoading={save.isPending} onClick={() => { setErr(""); save.mutate(); }}>저장</Button>
      </div>
    </div>
  );
}

/** 변경 이력(F-8) — 감사 로그 기반. 만기·단가 등 갱신 추적. */
function HistoryPanel({ path }: { path: string }) {
  const hist = useQuery({ queryKey: ["itam-history", path], queryFn: () => api<{ items: HistItem[] }>(path) });
  const fmt = (o: Record<string, unknown> | null) =>
    o ? Object.entries(o).map(([k, v]) => `${k}:${v ?? "-"}`).join(", ") : "";
  return (
    <div className="rounded-lg border border-slate-100 p-3">
      <p className="mb-2 text-sm font-medium text-slate-700">변경 이력</p>
      <Table headers={["시각", "동작", "이전", "이후"]} empty="이력 없음"
        rows={(hist.data?.items ?? []).map((h) => [
          <span key="t" className="text-xs">{h.at?.slice(0, 19).replace("T", " ")}</span>,
          h.action,
          <span key="b" className="text-[11px] text-slate-500">{fmt(h.before)}</span>,
          <span key="a" className="text-[11px] text-slate-700">{fmt(h.after)}</span>])} />
    </div>
  );
}
