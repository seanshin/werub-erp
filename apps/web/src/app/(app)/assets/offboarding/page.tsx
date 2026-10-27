"use client";
/** IT 퇴사 회수(ITAM M4) — 퇴사자 미회수 IT 자산·SW 좌석 목록·회수 처리. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { useConfirm } from "@/components/confirm";
import { useToast } from "@/components/toast";
import { AttachmentCell } from "@/components/attachment-cell";
import { EmployeePicker } from "@/components/employee-picker";
import { catKo } from "@/lib/itam";

interface Pending { employee_id: number; employee_no: string; name: string;
  resigned_on: string; asset_count: number; seat_count: number }
interface Holdings {
  assets: { id: number; asset_tag: string; name: string; category: string; status: string }[];
  seats: { allocation_id: number; service_id: number; service_name: string }[];
}
interface OffRecord { id: number; employee_id: number; reviewer_id: number | null;
  reviewer_name: string | null; handover_to_id: number | null; handover_to_name: string | null;
  status: string; note: string | null; completed_on: string | null }

export default function OffboardingPage() {
  const [sel, setSel] = useState<Pending | null>(null);
  const list = useQuery({ queryKey: ["itam-offboarding"],
    queryFn: () => api<{ items: Pending[] }>("/itam/offboarding") });

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-lg font-bold text-slate-900">IT 퇴사 회수</h1>
        <p className="text-xs text-slate-500">퇴사했으나 IT 자산·SW 좌석을 회수하지 못한 직원 (ITAM M4)</p>
      </div>
      {list.isError && <p className="text-sm text-red-600">조회 실패 — 권한(itam.assets)을 확인하세요</p>}
      <Card title={`미회수 퇴사자 — ${list.data?.items.length ?? 0}명`}>
        <Table headers={["사번", "이름", "퇴사일", "미회수 자산", "미회수 좌석", ""]}
          empty="미회수 항목이 없습니다 — 모두 회수 완료"
          rows={(list.data?.items ?? []).map((p) => [
            p.employee_no, p.name, p.resigned_on,
            p.asset_count ? <Badge key="a" tone="red">{p.asset_count}</Badge> : "0",
            p.seat_count ? <Badge key="s" tone="amber">{p.seat_count}</Badge> : "0",
            <Button key="d" size="sm" onClick={() => setSel(p)}>회수</Button>,
          ])} />
      </Card>
      {sel && <RecoverModal p={sel} onClose={() => setSel(null)} />}
    </div>
  );
}

function RecoverModal({ p, onClose }: { p: Pending; onClose: () => void }) {
  const qc = useQueryClient();
  const ask = useConfirm();
  const toast = useToast();
  const h = useQuery({ queryKey: ["itam-holdings", p.employee_id],
    queryFn: () => api<Holdings>(`/itam/employees/${p.employee_id}/holdings`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["itam-holdings", p.employee_id] });
    qc.invalidateQueries({ queryKey: ["itam-offboarding"] }); };
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.message : "회수 실패 — 다시 시도하세요");
  const ret = useMutation({
    mutationFn: (assetId: number) => api(`/itam/assets/${assetId}/return`, {
      method: "POST", body: JSON.stringify({}) }),
    onSuccess: () => { refresh(); }, onError: fail });
  const rel = useMutation({
    mutationFn: (allocId: number) => api(`/itam/service-allocations/${allocId}/release`, {
      method: "POST" }),
    onSuccess: () => { refresh(); }, onError: fail });

  const rec = useQuery({ queryKey: ["itam-offboarding-rec", p.employee_id],
    queryFn: () => api<{ record: OffRecord | null }>(`/itam/offboardings/${p.employee_id}`) });
  const refreshRec = () => qc.invalidateQueries({ queryKey: ["itam-offboarding-rec", p.employee_id] });
  const startRec = useMutation({
    mutationFn: () => api("/itam/offboardings", { method: "POST",
      body: JSON.stringify({ employee_id: p.employee_id }) }),
    onSuccess: () => { refreshRec(); }, onError: fail });
  const complete = useMutation({
    mutationFn: (id: number) => api(`/itam/offboardings/${id}/complete`, { method: "POST" }),
    onSuccess: () => { refreshRec(); refresh(); }, onError: fail });

  const d = h.data;
  const r = rec.data?.record ?? null;
  return (
    <Modal title={`${p.name} (${p.employee_no}) — IT 회수`} onClose={onClose} size="lg">
      {!d ? <p className="text-sm text-slate-400">불러오는 중…</p> : (
        <div className="space-y-4">

          <div className="rounded-lg border border-slate-100 p-3">
            <div className="mb-1 flex items-center justify-between">
              <p className="text-sm font-medium text-slate-700">퇴사 회수 기록</p>
              {r && (r.status === "COMPLETED"
                ? <Badge tone="green">완료 {r.completed_on ? `(${r.completed_on})` : ""}</Badge>
                : <Badge tone="amber">진행 중</Badge>)}
            </div>
            {!r ? (
              <Button isLoading={startRec.isPending} onClick={() => startRec.mutate()}>회수 기록 시작</Button>
            ) : (
              <OffboardingForm r={r} disabled={r.status === "COMPLETED"} onSaved={refreshRec} onFail={fail} />
            )}
          </div>

          <div>
            <p className="mb-1 text-sm font-medium text-slate-700">보유 IT 자산 ({d.assets.length})</p>
            <Table headers={["태그", "이름", "분류", ""]} empty="없음"
              rows={d.assets.map((a) => [
                <span key="t" className="font-mono text-xs">{a.asset_tag}</span>, a.name, catKo(a.category),
                <Button key="r" variant="warning" size="sm" onClick={() => ret.mutate(a.id)}>반납 처리</Button>,
              ])} />
          </div>
          <div>
            <p className="mb-1 text-sm font-medium text-slate-700">보유 SW 좌석 ({d.seats.length})</p>
            <Table headers={["서비스", ""]} empty="없음"
              rows={d.seats.map((s) => [
                s.service_name,
                <Button key="r" variant="neutral" size="sm" onClick={() => rel.mutate(s.allocation_id)}>좌석 회수</Button>,
              ])} />
          </div>
          {d.assets.length === 0 && d.seats.length === 0 &&
            <p className="text-sm text-emerald-600">✓ 자산·좌석 모두 회수 완료</p>}

          {r && r.status !== "COMPLETED" && (
            <div className="flex items-center gap-3">
              <Button isLoading={complete.isPending}
                onClick={async () => { if (await ask({ title: "퇴사 회수 완료",
                  body: "퇴사 회수를 완료 처리할까요?", confirmLabel: "완료 처리" })) complete.mutate(r.id); }}>
                회수 완료</Button>
              {(d.assets.length > 0 || d.seats.length > 0) &&
                <span className="text-[11px] text-amber-600">미회수 자산·좌석을 먼저 처리하세요.</span>}
            </div>
          )}
          <Button variant="secondary" onClick={onClose}>닫기</Button>
        </div>
      )}
    </Modal>
  );
}

/** 퇴사 회수 기록 폼 — 검토자·인수인계자·비고 + 실물 사진(itam_offboarding 갤러리). */
function OffboardingForm({ r, disabled, onSaved, onFail }: {
  r: OffRecord; disabled: boolean; onSaved: () => void; onFail: (e: unknown) => void }) {
  const [reviewer, setReviewer] = useState<number | null>(r.reviewer_id);
  const [handover, setHandover] = useState<number | null>(r.handover_to_id);
  const [note, setNote] = useState(r.note ?? "");
  const save = useMutation({
    mutationFn: () => api(`/itam/offboardings/${r.id}`, { method: "PATCH", body: JSON.stringify({
      reviewer_id: reviewer, handover_to_id: handover, note: note || null }) }),
    onSuccess: onSaved, onError: onFail });
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-3">
        <div><span className="mb-1 block text-xs font-medium text-slate-600">검토자</span>
          {disabled ? <p className="text-sm">{r.reviewer_name ?? "-"}</p>
            : <EmployeePicker value={reviewer} onPick={setReviewer} />}</div>
        <div><span className="mb-1 block text-xs font-medium text-slate-600">인수인계자</span>
          {disabled ? <p className="text-sm">{r.handover_to_name ?? "-"}</p>
            : <EmployeePicker value={handover} onPick={setHandover} />}</div>
      </div>
      {disabled ? (r.note && <p className="text-sm text-slate-600"><b>비고</b> {r.note}</p>) : (
        <Input label="비고(회수 상태·인수인계 사항)" value={note} onChange={(e) => setNote(e.target.value)} />
      )}
      {!disabled && <Button isLoading={save.isPending} onClick={() => save.mutate()}>기록 저장</Button>}
      <div>
        <p className="mb-1 text-xs font-medium text-slate-600">📷 실물 사진·회수 증빙</p>
        <AttachmentCell entityType="itam_offboarding" entityId={r.id} gallery />
      </div>
    </div>
  );
}
