"use client";
/** 재무 — AP 지급결의 배치(펌뱅킹 이체·P3-4): 미지급금 다건 결의→승인(SoD)→실행(전표·이체파일).
 *  결의/승인/실행은 관리자(finance.settlement write=ADMIN). 이체파일=표준 CSV. */
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ApiError, api, apiDownload, auth } from "@/lib/api";
import { Badge, Button, Card, Input, Modal, Table } from "@/components/ui";
import { QueryErrorBanner } from "@/components/query-guard";
import { won } from "@/lib/format";
import { todayLocal } from "@/lib/dates";
import { useConfirm } from "@/components/confirm";
import { useInputDialog } from "@/components/input-dialog";
import { useToast } from "@/components/toast";

const ST: Record<string, { kr: string; tone: "gray" | "blue" | "green" | "red" }> = {
  DRAFT: { kr: "결의(작성)", tone: "gray" }, APPROVED: { kr: "승인", tone: "blue" },
  EXECUTED: { kr: "지급완료", tone: "green" }, CANCELED: { kr: "취소", tone: "red" } };

interface Payable {
  id: number; party: string; description: string | null; amount: number;
  paid: number; outstanding: number; due_on: string; status: string;
}
interface Batch {
  id: number; batch_no: string; status: string; scheduled_on: string;
  total_amount: number; line_count: number; note: string | null; executed_on: string | null;
}
interface Line {
  id: number; payable_id: number; party: string; amount: number;
  due_on: string | null; bank_account: string | null; status: string;
}
interface Detail extends Batch { lines: Line[] }

export default function DisbursementsPage() {
  const qc = useQueryClient();
  const toast = useToast();
  const confirm = useConfirm();
  const { ask, dialog } = useInputDialog();
  const isAdmin = auth.claims()?.role === "ADMIN";
  const fail = (e: unknown) => toast.err(e instanceof ApiError ? e.error.message : "처리 실패");
  const [sel, setSel] = useState<Set<number>>(new Set());
  const [creating, setCreating] = useState(false);
  const [detailId, setDetailId] = useState<number | null>(null);

  const payables = useQuery({ queryKey: ["ap-open"], queryFn: () =>
    api<{ items: Payable[] }>("/finance/payables?size=200") });
  const batches = useQuery({ queryKey: ["disb-list"], queryFn: () =>
    api<{ items: Batch[] }>("/finance/disbursements") });
  const detail = useQuery({ queryKey: ["disb", detailId], enabled: detailId != null, queryFn: () =>
    api<Detail>(`/finance/disbursements/${detailId}`) });
  const refresh = () => { qc.invalidateQueries({ queryKey: ["ap-open"] });
    qc.invalidateQueries({ queryKey: ["disb-list"] });
    if (detailId != null) qc.invalidateQueries({ queryKey: ["disb", detailId] }); };

  const approve = useMutation({
    mutationFn: (id: number) => api(`/finance/disbursements/${id}/approve`, { method: "POST" }),
    onSuccess: () => { toast.ok("승인"); refresh(); }, onError: fail });
  const cancel = useMutation({
    mutationFn: (id: number) => api(`/finance/disbursements/${id}/cancel`, { method: "POST" }),
    onSuccess: () => { toast.ok("취소"); refresh(); }, onError: fail });
  const execute = useMutation({
    mutationFn: (v: { id: number; on: string }) =>
      api<{ paid_total: number }>(`/finance/disbursements/${v.id}/execute?on=${v.on}`, { method: "POST" }),
    onSuccess: (r) => { toast.ok(`지급 실행 — ${won(r.paid_total)} (DR 미지급/CR 현금)`); refresh(); },
    onError: fail });

  const onExecute = async (b: Batch) => {
    const on = await ask({ title: "지급 실행", label: "지급일", kind: "date",
      defaultValue: todayLocal(), confirmLabel: "실행" });
    if (on && await confirm({ title: "지급 실행 확인", danger: true, confirmLabel: "실행",
      body: `${b.batch_no} — ${won(b.total_amount)} 지급 전기(각 미지급금 DR 미지급/CR 현금)합니다.` }))
      execute.mutate({ id: b.id, on: String(on) });
  };
  const onCancel = async (b: Batch) => {
    if (await confirm({ title: "지급결의 취소", danger: true, confirmLabel: "취소",
      body: `${b.batch_no}를 취소합니다(미지급금 재선택 가능).` })) cancel.mutate(b.id);
  };

  const toggle = (id: number) => setSel((s) => {
    const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const openPayables = (payables.data?.items ?? []).filter((p) => p.status !== "PAID");
  const selTotal = openPayables.filter((p) => sel.has(p.id)).reduce((a, p) => a + p.outstanding, 0);
  const d = detail.data;

  return (
    <div className="space-y-4">
      {dialog}
      <h1 className="text-lg font-bold">재무 — AP 지급결의 배치</h1>
      <p className="text-xs text-slate-500">
        미지급금 다건을 지급결의로 묶어 승인(직무분리)→실행(지급 전기 DR 미지급/CR 현금)합니다.
        이체파일(표준 CSV)로 펌뱅킹 업로드. 결의·승인·실행은 관리자.</p>

      <QueryErrorBanner queries={[payables, batches]} />

      {isAdmin && (
        <Card title={`미지급금 선택 (선택 ${sel.size}건 · ${won(selTotal)})`} action={
          <Button size="sm" disabled={sel.size === 0} onClick={() => setCreating(true)}>지급결의 생성</Button>}>
          <Table headers={["", "거래처", "내역", "미지급잔액", "만기", "상태"]}
            rows={openPayables.map((p) => [
              <input key="c" type="checkbox" checked={sel.has(p.id)} onChange={() => toggle(p.id)} />,
              p.party, p.description ?? "—", won(p.outstanding), p.due_on,
              <Badge key="s" tone={p.status === "PARTIAL" ? "amber" : "gray"}>
                {p.status === "PARTIAL" ? "부분지급" : "미지급"}</Badge>,
            ])} empty={payables.isFetching ? "불러오는 중…" : "미지급금 없음"} />
        </Card>
      )}

      <Card title="지급결의 목록">
        <Table headers={["결의번호", "예정일", "건수", "금액", "상태", ""]}
          rows={(batches.data?.items ?? []).map((b) => {
            const st = ST[b.status] ?? { kr: b.status, tone: "gray" as const };
            return [
              <button key="n" className="text-blue-600 hover:underline"
                onClick={() => setDetailId(b.id)}>{b.batch_no}</button>,
              b.scheduled_on, `${b.line_count}건`, won(b.total_amount),
              <Badge key="s" tone={st.tone}>{st.kr}</Badge>,
              <span key="a" className="flex flex-wrap gap-1">
                {isAdmin && b.status === "DRAFT" && (<>
                  <Button size="sm" disabled={approve.isPending} onClick={() => approve.mutate(b.id)}>승인</Button>
                  <Button size="sm" variant="danger" disabled={cancel.isPending} onClick={() => onCancel(b)}>취소</Button>
                </>)}
                {isAdmin && b.status === "APPROVED" && (<>
                  <Button size="sm" disabled={execute.isPending} onClick={() => onExecute(b)}>지급 실행</Button>
                  <Button size="sm" variant="danger" disabled={cancel.isPending} onClick={() => onCancel(b)}>취소</Button>
                </>)}
                {(b.status === "APPROVED" || b.status === "EXECUTED") && (
                  <Button key="f" size="sm" variant="neutral"
                    onClick={() => apiDownload(`/finance/disbursements/${b.id}/file`, `${b.batch_no}.csv`)}>이체파일</Button>)}
              </span>,
            ];
          })} empty={batches.isFetching ? "불러오는 중…" : "지급결의 없음"} />
      </Card>

      {detailId != null && (
        <Modal title={d ? `${d.batch_no} 상세` : "지급결의 상세"} size="lg" onClose={() => setDetailId(null)}>
          {!d ? <p className="text-sm text-slate-400">불러오는 중…</p> : (
            <div className="space-y-2">
              <p className="text-sm">
                <Badge tone={(ST[d.status] ?? { tone: "gray" as const }).tone}>{(ST[d.status] ?? { kr: d.status }).kr}</Badge>
                <span className="ml-2 text-slate-600">예정 {d.scheduled_on} · {won(d.total_amount)} · {d.line_count}건
                  {d.executed_on ? ` · 지급 ${d.executed_on}` : ""}</span></p>
              <Table headers={["거래처", "미지급ID", "금액", "만기", "계좌", "상태"]}
                rows={d.lines.map((ln) => [
                  ln.party, `#${ln.payable_id}`, won(ln.amount), ln.due_on ?? "—",
                  ln.bank_account ?? "—",
                  <Badge key="s" tone={ln.status === "PAID" ? "green" : "gray"}>
                    {ln.status === "PAID" ? "지급" : "대기"}</Badge>,
                ])} empty="라인 없음" />
            </div>
          )}
        </Modal>
      )}

      {creating && <CreateModal payables={openPayables.filter((p) => sel.has(p.id))}
        onDone={() => { setCreating(false); setSel(new Set()); refresh(); }} onError={fail}
        onClose={() => setCreating(false)} />}
    </div>
  );
}

function CreateModal({ payables, onDone, onError, onClose }: {
  payables: Payable[]; onDone: () => void; onError: (e: unknown) => void; onClose: () => void;
}) {
  const toast = useToast();
  const [scheduledOn, setScheduledOn] = useState(todayLocal());
  const [note, setNote] = useState("");
  const total = payables.reduce((a, p) => a + p.outstanding, 0);
  const m = useMutation({
    mutationFn: () => api<{ batch_no: string }>("/finance/disbursements", { method: "POST",
      body: JSON.stringify({ payable_ids: payables.map((p) => p.id), scheduled_on: scheduledOn,
        note: note || null }) }),
    onSuccess: (r) => { toast.ok(`지급결의 생성 — ${r.batch_no}`); onDone(); }, onError });
  return (
    <Modal title="지급결의 생성" dirty onClose={onClose}>
      <div className="space-y-2">
        <p className="text-sm text-slate-600">{payables.length}건 · 합계 {won(total)}</p>
        <Input label="지급 예정일" type="date" value={scheduledOn}
          onChange={(e) => setScheduledOn(e.target.value)} />
        <Input label="비고(선택)" value={note} onChange={(e) => setNote(e.target.value)} />
        <Button disabled={m.isPending || payables.length === 0} onClick={() => m.mutate()}>결의 생성</Button>
      </div>
    </Modal>
  );
}
